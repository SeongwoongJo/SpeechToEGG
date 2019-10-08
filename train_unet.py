import csv
import numpy as np
import time
from tqdm import tqdm

import torch
from torch import nn
from torch.utils import data
from torch.optim.lr_scheduler import StepLR
from torch.nn import functional as F

from radam import RAdam,Lookahead
from utils import *
from dataloader import *
from model_unet import Unet,Resv2Unet
from apex import amp
# from apex.parallel import DistributedDataParallel as DDP
import multiprocessing as mp

seed_everything(42)
###Hyper parameters

save_path = './models/Resv2Unet_heavy/'
os.makedirs(save_path,exist_ok=True)
n_epoch = 100
batch_size = 6000
n_frame = 192
window = int(n_frame*1.4)
step = int(n_frame/2.5)
learning_rate = 1e-2

print("[*] load data ...")
st = time.time()
train_X,train_y,val_X,val_y = load_datas(n_frame,window,step)

print(train_X.shape)
print(train_y.shape)
print(val_X.shape)
print(val_y.shape)

train_dataset = Dataset(train_X,train_y,n_frame = n_frame, is_train = True, aug = custom_aug)
valid_dataset = Dataset(val_X,val_y,n_frame = n_frame, is_train = False)
train_loader = data.DataLoader(dataset=train_dataset,
                               batch_size=batch_size,
                               num_workers=mp.cpu_count(),
                               shuffle=True)
valid_loader = data.DataLoader(dataset=valid_dataset,
                               batch_size=batch_size,
                               num_workers=mp.cpu_count(),
                              shuffle=False)

print("Load duration : {}".format(time.time()-st))
print("[!] load data end")

model = Resv2Unet(nlayers = 5, nefilters = 32,filter_size = 15,merge_filter_size = 5)
model.cuda()

# criterion = nn.MSELoss()
criterion = CosineDistanceLoss()
# criterion.cuda()
# optimizer = torch.optim.Adam(model.parameters(), lr= learning_rate)
optimizer = RAdam(model.parameters(), lr= learning_rate)
optimizer = Lookahead(optimizer, alpha=0.5, k=5)

opt_level = 'O1'
model,optimizer = amp.initialize(model,optimizer,opt_level = opt_level)
scheduler = StepLR(optimizer,step_size=80,gamma = 0.1)
model = nn.DataParallel(model)

print("[*] training ...")
log = open(os.path.join(save_path,'log.csv'), 'w', encoding='utf-8', newline='')
log_writer = csv.writer(log)

best_val = 100.
for epoch in range(n_epoch):
    train_loss = 0.
    optimizer.zero_grad()
    model.train()
    for idx,(_x,_y) in enumerate(tqdm(train_loader)):
        x_train,y_train = _x.float().cuda(),_y.float().cuda()
        pred = model(x_train)

#         pred_diff = pred[:,1:,:]-pred[:,:-1,:]
#         y_train_diff = y_train[:,1:,:] - y_train[:,:-1,:]
        
#         loss = criterion(pred,y_train) + criterion(pred_diff,y_train_diff)
        loss = criterion(pred,y_train)
    
        with amp.scale_loss(loss, optimizer) as scaled_loss:
            scaled_loss.backward()
#         loss.backward()
        optimizer.step()
        optimizer.zero_grad()
        train_loss += loss.item() / len(train_loader)
    
    val_loss = 0.
    model.eval()
    with torch.no_grad():
        for idx,(_x,_y) in enumerate(tqdm(valid_loader)):
            x_val,y_val = _x.float().cuda(),_y.float().cuda()
            pred = model(x_val)
            
#             pred_diff = pred[:,1:,:]-pred[:,:-1,:]
#             y_val_diff = y_val[:,1:,:] - y_val[:,:-1,:]
        
#             loss = criterion(pred,y_val) + criterion(pred_diff,y_val_diff)
            loss = criterion(pred,y_val)
    
            val_loss += loss.item()/len(valid_loader)
    
    scheduler.step()
    
    if val_loss < best_val:
        torch.save(model.state_dict(), os.path.join(save_path,'best-cosloss-Ranger.pth'))
        best_val = val_loss
        
    log_writer.writerow([epoch,train_loss,val_loss])
    log.flush()
    print("Epoch [%d]/[%d] train_loss %.6f valid_loss %.6f "%
          (epoch,n_epoch,train_loss,val_loss))

log.close()
print("[!] training end")