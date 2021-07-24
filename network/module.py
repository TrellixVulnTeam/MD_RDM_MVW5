from unicodedata import normalize
from numpy.lib import utils
import torch
from torchvision import transforms
import torchvision.transforms.functional as TF
import pytorch_lightning as pl
import numpy as np
from torch import cuda
from metrics import MetricLogger
from network.RDM_Net import DepthEstimationNet
from network import computations as cp
import utils as u
import loss as l
from dataloaders.nyu_dataloader import NYUDataset

is_cuda=False
class RelativeDephModule(pl.LightningModule):
    def __init__(self, path, dataset_type, batch_size, learning_rate, worker, metrics, limits, config, gpus, *args, **kwargs):
        super().__init__()
        self.save_hyperparameters()
        self.metric_logger = MetricLogger(metrics=metrics, module=self)
        self.train_loader = torch.utils.data.DataLoader(NYUDataset(path, dataset_type=dataset_type, split="train", output_size=(226, 226)),
                                                    batch_size=batch_size, 
                                                    shuffle=True, 
                                                    num_workers=worker, 
                                                    pin_memory=True)
        self.val_loader = torch.utils.data.DataLoader(NYUDataset(path, dataset_type='labeled', split="val", output_size=(226, 226)),
                                                    batch_size=1, 
                                                    shuffle=False, 
                                                    num_workers=worker, 
                                                    pin_memory=True) 
        self.criterion = torch.nn.MSELoss()
        self.limits = limits
        is_cuda = gpus > 0
        print("Use cuda: {0}".format(is_cuda))
        if is_cuda:
            self.model = DepthEstimationNet(config, gpus).cuda()
        else:
            self.model = DepthEstimationNet(config, gpus)

        

    def configure_optimizers(self):
        train_param = self.model.parameters()
        # Training parameters
        optimizer = torch.optim.AdamW(train_param, lr=self.hparams.learning_rate)
        lr_scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(optimizer, 'max', patience=2)
        scheduler = {
            'scheduler': lr_scheduler,
            'monitor': 'val_delta1'
        }
        return [optimizer], [scheduler]

    def forward(self, x):

        if is_cuda:
            x=x.cuda()

        fine_details, d_pred, l_pred = self.model(x)

        return fine_details, d_pred, l_pred

    def train_dataloader(self):
        return self.train_loader

    def val_dataloader(self):
        return self.val_loader                                            

    def training_step(self, batch, batch_idx):
        if batch_idx == 0: self.metric_logger.reset()
        x, y = batch
        
        y = cp.resize(y,128)

        if is_cuda:
            y = y.cuda() 
            #x = x.cuda()

        #mask target
        gt = y
        mask1 = y > 0
        mask2 = (y <= 0) + 1e-4
        y = (gt * mask1) + mask2

        fine_details, ord_depth_pred, ord_label_pred = self(x)
        ord_y = self.compute_ordinal_target(ord_depth_pred, y)
        ord_loss = l.Ordinal_Loss().calc(ord_label_pred, ord_y, cuda=is_cuda)        
        final_depth, fine_detail_loss = self.compute_final_depth(fine_details, y)
        final_depth = torch.exp(final_depth)
        mse = self.criterion(final_depth, y)
        loss_all = mse + ord_loss + fine_detail_loss
 
       
        self.log("MSE", mse, prog_bar=True)
        self.log("Ord_Loss", ord_loss, prog_bar=True)
        self.log("Fine_Detail", fine_detail_loss, prog_bar=True)             
        return self.metric_logger.log_train(final_depth, self.normalize(y), loss_all)

    def validation_step(self, batch, batch_idx):
        if batch_idx == 0: self.metric_logger.reset()
        x, y = batch
        y_origin = y
        y = cp.resize(y,128)

        if is_cuda:
            y = y.cuda() 
            #x = x.cuda()
        
        norm = self.normalize(y)

        #mask target
        gt = y
        mask1 = y > 0
        mask2 = (y <= 0) + 1e-4
        y = (gt * mask1) + mask2
        fine_details, _, _ = self(x)
        y_hat, _ = self.compute_final_depth(fine_details, y)
        y_hat = torch.exp(y_hat)
        self.save_visual(x, y_origin, u.adjust_padding(y_hat), batch_idx)
        self.switch_config(self.current_epoch)
        return self.metric_logger.log_val(y_hat, norm)
    
    def compute_final_depth(self, fine_detail_list, target):
        #decompose target map
        B,C,H,W = target.size()

        component_target = cp.decomp(self.normalize(target), 7)[::-1]
        tmp = cp.alt_resize(target, n=4)
        # print("Sid nan: {0}".format(torch.isnan(u.depth2label_sid(tmp, cuda=is_cuda)).any()))
        # print("Sid < 0: {0}".format((u.depth2label_sid(tmp, cuda=is_cuda) < 0).any()))
        # print("Normalized Sid nan: {0}".format(self.normalize(u.depth2label_sid(tmp, cuda=is_cuda))))
        #tmp = tmp * (tmp > 0)
        ord_components = cp.decomp(self.normalize(u.depth2label_sid(tmp, cuda=is_cuda)), 3)[::-1]
        component_target[0] = ord_components[0]
        component_target = [torch.log(x) for x in component_target]
        #optimize weight layer
        components, loss = cp.optimize_components(fine_detail_list, component_target, is_cuda)
        #returns optimal candidates are recombined to final depth map
        final = cp.recombination(components)
        return final,loss
    
    def compute_ordinal_target(self, ord_pred, target):
        #resize target to correct size
        target = cp.resize(target, ord_pred.shape[2])
        if is_cuda:
            target = target.cuda()
        #print(target.shape)
        #transform with ordinal regression so it can be compared
        ord_target = u.depth2label_sid(target, cuda=is_cuda)
        return ord_target
    
    def normalize(self, batch):
        B,C,H,W = batch.size()
        if is_cuda:
            return torch.div(batch,cp.quick_gm(batch.view(B,H*W,1), H).expand(B,H*W).view(B,1,H,W)).cuda()
        return torch.div(batch,cp.quick_gm(batch.view(B,H*W,1), H).expand(B,H*W).view(B,1,H,W))
        #return torch.div(batch,cp.quick_gm(batch.view(B,H*W,1), H).expand(B,H*W).view(B,1,H,W)) 
    
    def switch_config(self, epoch):
        if epoch == self.limits[0]:
            self.model.freeze_encoder()
            self.model.update_config([1,0,0,0,0,1,0,0,0,0])
        elif epoch == self.limits[1]:
            self.model.update_config([1,0,0,0,0,1,1,0,0,0])
        elif epoch == self.limits[2]:
            self.model.update_config([1,0,0,0,0,1,1,1,0,0])
        elif epoch == self.limits[3]:
            self.model.update_config([1,0,0,0,0,1,1,1,1,0])

    def save_visual(self, x, y, y_hat, batch_idx):
        if batch_idx == 0:
            self.img_merge = u.merge_into_row(x, y, y_hat)
        elif (batch_idx < 8 * self.skip) and (batch_idx % self.skip == 0):
            row = u.merge_into_row(x, y, y_hat)
            self.img_merge = u.add_row(self.img_merge, row)
        elif batch_idx == 8 * self.skip:
            filename = "{}/{}/version_{}/epoch{}.jpg".format(self.logger.save_dir, self.logger.name, self.logger.version, self.current_epoch)
            u.save_image(self.img_merge, filename)
