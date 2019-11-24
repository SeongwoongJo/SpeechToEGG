import csv
import time
import multiprocessing as mp
import argparse
import warnings
from tqdm import tqdm
import numpy as np

import torch
from torch import nn
from torch.utils import data
from torch.optim.lr_scheduler import StepLR
# from torch.utils.tensorboard import SummaryWriter
from tensorboardX import SummaryWriter
from torchvision.utils import make_grid
from torch.nn import functional as F
from stft_utils.stft import STFT
import torch.distributed as dist

from radam import RAdam,Lookahead
from cds_utils import *
from cds_utils import _get_CQSQ
from cds_dataloader import *
from cds_models import MMDenseNet, Vgg16

import apex
from apex import amp
from apex.parallel import DistributedDataParallel as DDP
from sync_batchnorm import convert_model

seed_everything(42)
###Hyper parameters

parser = argparse.ArgumentParser()
parser.add_argument('--batch_size', type=int, default = 8192)
parser.add_argument('--epoch',type=int, default = 100)
parser.add_argument('--beta1',type=float,default = 0.9)
parser.add_argument('--beta2',type=float,default = 0.999)
parser.add_argument('--weight_decay',type=float,default = 1e-5)
parser.add_argument('--lr', type=float, default = 3e-4)
parser.add_argument('--lr_step', type=int, default = 65)
parser.add_argument('--exp_num',type=str,default='0')
parser.add_argument('--n_frame',type=int,default=10000)
parser.add_argument('--n_fft',type=int,default=512)
parser.add_argument('--hop_length',type=int,default=128)
parser.add_argument('--scheduler_gamma',type=float,default=0.1)
parser.add_argument('--test',action='store_true')
parser.add_argument('--start_epoch',type=int,default=0)
parser.add_argument('--mode',type=str,default ='ddp')
parser.add_argument('--mixed',action='store_true')
parser.add_argument('--local_rank',type=int,default=0)

args = parser.parse_args()

##train ? or test? 
is_test = args.test
mixed = args.mixed
mode = args.mode
start_epoch = args.start_epoch
##training parameters
n_epoch = args.epoch if not is_test else start_epoch+1
batch_size = args.batch_size//4 if mode == 'ddp' else args.batch_size
beta1 = args.beta1
beta2 = args.beta2
weight_decay = args.weight_decay

##data preprocessing parameters##
n_frame = args.n_frame
n_fft = args.n_fft
hop_length = args.hop_length

##optimizer parameters##
learning_rate = args.lr

##scheduler parameters## 
step_size = args.lr_step - start_epoch
scheduler_gamma = args.scheduler_gamma

##saving path
save_path = './models/fft/exp{}/'.format(args.exp_num)
os.makedirs(save_path,exist_ok=True)

##Distributed Data Parallel
if mode == 'ddp':
    torch.cuda.set_device(args.local_rank)
    torch.distributed.init_process_group(backend='nccl',
                                         init_method='env://')
    args.world_size = torch.distributed.get_world_size()
    
verbose = 1 if args.local_rank ==0 else 0

if not verbose:
    warnings.filterwarnings(action='ignore')
logging = print_verbose(verbose)
logging("[*] load data ...")

st = time.time()
# train_X,train_y,val_X,val_y = load_datas(n_frame,window,step,top_db,is_test,hvd.local_rank())
train,val = load_stft_datas_path(is_test)
normal_noise,musical_noise = load_stft_noise(is_test)

logging(len(train))
logging(len(val))

# train_dataset = Dataset(train_X,train_y,n_fft,hop_length,n_frame,normal_noise=normal_noise,musical_noise=musical_noise,is_train = True, aug = custom_aug_v3(n_frame))
# valid_dataset = Dataset(val_X,val_y,n_fft,hop_length,n_frame, is_train = False)
train_dataset = STFTDataset(train,n_frame,normal_noise=normal_noise,musical_noise=musical_noise,is_train = True, aug = custom_stft_aug(n_frame))
valid_dataset = STFTDataset(val,n_frame, is_train = False)
train_sampler = torch.utils.data.distributed.DistributedSampler(train_dataset, num_replicas=4, rank=args.local_rank)
valid_sampler = torch.utils.data.distributed.DistributedSampler(valid_dataset, num_replicas=4, rank=args.local_rank)
train_loader = data.DataLoader(dataset=train_dataset,
                               batch_size=batch_size,
                               num_workers=mp.cpu_count()//4 if mode == 'ddp' else mp.cpu_count(),
                               sampler = train_sampler if mode == 'ddp' else None,
                               shuffle = True if mode !='ddp' else False,
                               pin_memory=True)
valid_loader = data.DataLoader(dataset=valid_dataset,
                               batch_size=batch_size,
                               num_workers=mp.cpu_count()//4 if mode == 'ddp' else mp.cpu_count(),
                               sampler = valid_sampler if mode == 'ddp' else None,
                               pin_memory=True)
logging("Load duration : {}".format(time.time()-st))
logging("[!] load data end")

MSE_criterion = nn.MSELoss(reduction='mean')
spec_criterion = spectral_loss(coeff = [0,1,1,0])
cds_criterion = CosineDistanceLoss()
L1_criterion = nn.L1Loss()

model = MMDenseNet(drop_rate=0.2,bn_size=4,k=12,l=3)
if mode == 'ddp':
#     model = apex.parallel.convert_syncbn_model(model)
#     model = torch.nn.SyncBatchNorm.convert_sync_batchnorm(model)
    model = convert_model(model)
model.cuda()

optimizer = RAdam(model.parameters(), lr=learning_rate, betas=(beta1, beta2), eps=1e-8, weight_decay=weight_decay)
optimizer = Lookahead(optimizer,alpha=0.5,k=6)
if mixed:
    model,optimizer = amp.initialize(model,optimizer,opt_level = 'O1')
scheduler = StepLR(optimizer,step_size=step_size,gamma = scheduler_gamma)

if mode == 'ddp':
    model = torch.nn.parallel.DistributedDataParallel(model,
                                                      device_ids=[args.local_rank],
                                                      output_device=0)    
else:
    model = torch.nn.DataParallel(model)

"""
vgg model for computing composite spectrogram loss
"""
vgg =  Vgg16(requires_grad=False)
if mode == 'ddp':
#     model = apex.parallel.convert_syncbn_model(model)
#     model = torch.nn.SyncBatchNorm.convert_sync_batchnorm(model)
    vgg = convert_model(vgg)
vgg.cuda()

if mode == 'ddp':
    vgg = torch.nn.parallel.DistributedDataParallel(vgg,
                                                      device_ids=[args.local_rank],
                                                      output_device=0)    
else:
    vgg = torch.nn.DataParallel(vgg)    

"""
Training
"""
    
logging("[*] training ...")
if verbose and not is_test:
#     log = open(os.path.join(save_path,'log.csv'), 'a+', encoding='utf-8', newline='')
#     log_writer = csv.writer(log)
    writer = SummaryWriter('../log/%s/'%args.exp_num)
#     hparams = {'batch_size' : batch_size, 'n_frame' : n_frame, 'lr' : learning_rate,'lr_step' : step_size,'gamma':scheduler_gamma,'beta1':beta1,'beta2':beta2, 'weight_decay' : weight_decay}
    
#     writer.add_hparams(hparams,{})
    
    best_val = np.inf
    best_metric = np.inf
# stft = STFT(filter_length = n_fft, hop_length = hop_length,window='hann').cuda()
# pool = mp.Pool(mp.cpu_count()//4 if mode == 'ddp' else mp.cpu_count())

if start_epoch >0:
    model.load_state_dict(torch.load(save_path+"best.pth"))
    
for epoch in range(start_epoch,n_epoch): 
    st = time.time()
    train_sampler.set_epoch(epoch)
    train_loss = 0.
    train_L2_loss = 0.
    train_content_loss = 0.
    train_style_loss = 0.
    
#     train_CQ_diff = 0.
#     train_SQ_diff = 0.
    optimizer.zero_grad()
    model.train()
    
    for idx,(_x,_y) in enumerate(tqdm(train_loader,disable=(verbose==0))):
        optimizer.zero_grad()
        x_train,y_train = _x.cuda(),_y.cuda()
        pred = model(x_train)
        vgg_input_pred = normalize_batch(pred)
        vgg_input_y = normalize_batch(y_train)
        print(0)
        pred_feature = vgg(vgg_input_pred)
        print(1)
        true_feature = vgg(vgg_input_y)
        print(2)
        gram_style = [gram_matrix(y) for y in true_feature]
        print(3)
        L2_loss = MSE_criterion(pred,y_train)
        print(4)
        content_loss = MSE_criterion(pred_feature.relu2_2,true_feature.relu2_2)
        print(5)
        style_loss = 0.
        for ft_pred,gm_s in zip(pred_feature,gram_style):
            gm_y = gram_matrix(ft_pred)
            style_loss += mse_loss(gm_y,gm_s)
        
        loss = 0.5*L2_loss + 0.25*content_loss + 0.25*style_loss
        
        if mixed:
            with amp.scale_loss(loss, optimizer) as scaled_loss:
                scaled_loss.backward()
        else:
            loss.backward()
#         torch.nn.utils.clip_grad_norm_(amp.master_params(optimizer), 5)
        optimizer.step()
        
        if mode == 'ddp':
            reduced_loss = reduce_tensor(loss.data)
            train_loss += reduced_loss.item() / len(train_loader)
            reduced_L2_loss = reduce_tensor(L2_loss.data)
            train_L2_loss += reduced_L2_loss.item() / len(train_loader)
            reduced_content_loss = reduce_tensor(content_loss.data)
            train_content_loss += reduced_content_loss.item() / len(train_loader)
            reduced_style_loss = reduce_tensor(style_loss.data)
            train_style_loss += reduced_style_loss.item() / len(train_loader)
        else:
            train_loss += loss.item() / len(train_loader)
            train_L2_loss += L2_loss.item() / len(train_loader)
            train_content_loss += train_content_loss.item() / len(train_loader)
            train_style_loss += train_style_loss.item() / len(train_loader)
            
#         CQ_diff,SQ_diff = metric(pred.cpu().detach().numpy(),y_train.cpu().detach().numpy(),hop_length,n_fft)
#         if mode == 'ddp':
#             CQ_diff = reduce_tensor(torch.Tensor([CQ_diff]).cuda())
#             SQ_diff = reduce_tensor(torch.Tensor([SQ_diff]).cuda())
#         train_CQ_diff += CQ_diff.item()/len(train_loader)
#         train_SQ_diff += SQ_diff.item()/len(train_loader)
        
    val_loss = 0.
    val_L2_loss = 0.
    val_content_loss = 0.
    val_style_loss = 0.
#     valid_CQ_diff_avg =0
#     valid_CQ_diff_std = 0
#     valid_SQ_diff_avg = 0
#     valid_SQ_diff_std = 0
    
    model.eval()
    for idx,(_x,_y) in enumerate(tqdm(valid_loader,disable=(verbose==0))):
        x_val,y_val = _x.cuda(),_y.cuda()
        
        with torch.no_grad():
            pred = model(x_val)
            vgg_input_pred = normalize_batch(pred)
            vgg_input_y = normalize_batch(y_val)
            pred_feature = vgg(normalize_batch(vgg_input_pred))
            true_feature = vgg(normalize_batch(vgg_input_y))
            gram_style = [gram_matrix(y) for y in true_feature]

            L2_loss = MSE_criterion(pred,y_val)
            content_loss = MSE_criterion(pred_feature.relu2_2,true_feature.relu2_2)
            style_loss = 0.
            for ft_pred,gm_s in zip(pred_feature,gram_style):
                gm_y = gram_matrix(ft_pred)
                style_loss += mse_loss(gm_y,gm_s)
        
            loss = 0.5*L2_loss + 0.25*content_loss + 0.25*style_loss
            
        if mode == 'ddp':
            reduced_loss = reduce_tensor(loss.data)
            val_loss += reduced_loss.item()/len(valid_loader)
            
            reduced_L2_loss = reduce_tensor(L2_loss.data)
            val_L2_loss += reduced_L2_loss.item() / len(valid_loader)
            reduced_content_loss = reduce_tensor(content_loss.data)
            val_content_loss += reduced_content_loss.item() / len(valid_loader)
            reduced_style_loss = reduce_tensor(style_loss.data)
            val_style_loss += reduced_style_loss.item() / len(valid_loader)
        else:
            val_loss += loss.item()/len(valid_loader)
            val_loss += L2_loss.item()/len(valid_loader)
            val_content_loss += val_content_loss.item() / len(valid_loader)
            val_style_loss += val_style_loss.item() / len(valid_loader)
            
#         pred_egg = pred_egg.cpu().detach().numpy()
#         y_val = y_val.cpu().detach().numpy()
        
#         CQ_diff_avg,SQ_diff_avg,CQ_diff_std,SQ_diff_std = metric_egg(pred_egg,y_val,hop_length,n_fft,n_frame)
#         if mode == 'ddp':
#             CQ_diff_avg = reduce_tensor(torch.Tensor([CQ_diff_avg]).cuda())
#             CQ_diff_std = reduce_tensor(torch.Tensor([CQ_diff_std]).cuda())
#             SQ_diff_avg = reduce_tensor(torch.Tensor([SQ_diff_avg]).cuda())
#             SQ_diff_std = reduce_tensor(torch.Tensor([SQ_diff_std]).cuda())
#         valid_CQ_diff_avg += CQ_diff_avg.item()/len(valid_loader)
#         valid_SQ_diff_avg += SQ_diff_avg.item()/len(valid_loader)
#         valid_CQ_diff_std += CQ_diff_std.item()/len(valid_loader)
#         valid_SQ_diff_std += SQ_diff_std.item()/len(valid_loader)
        
    if verbose and not is_test:
        if val_loss < best_val:
            torch.save(model.state_dict(), os.path.join(save_path,'best.pth'))
            best_val = val_loss
            
            pred = torch.cat([pred,pred,pred],dim=1)
            y_val = torch.cat([y_val,y_val,y_val],dim=1)
            pred = make_grid(pred, normalize=True, scale_each=True)
            y_val = make_grid(y_val, normalize=True, scale_each=True)
            writer.add_image('img_pred/mel', pred, epoch)
            writer.add_image('img_true/mel', y_val, epoch)
            writer.add_scalar('Loss/train', train_loss, epoch)
            writer.add_scalar('Loss/val',val_loss,epoch)
            writer.add_scalar('pixel_L2_Loss/train', train_L2_loss, epoch)
            writer.add_scalar('pixel_L2_Loss/val',val_L2_loss,epoch)
            writer.add_scalar('pixel_content_Loss/train', train_content_loss, epoch)
            writer.add_scalar('pixel_content_Loss/val',val_content_loss,epoch)
            writer.add_scalar('pixel_style_Loss/train', train_style_loss, epoch)
            writer.add_scalar('pixel_style_Loss/val',val_style_loss,epoch)
#         writer.add_scalar('CQ_diff_avg/val',valid_CQ_diff_avg)
#         writer.add_scalar('CQ_diff_std/val',valid_CQ_diff_std)
#         writer.add_scalar('SQ_diff_avg/val',valid_SQ_diff_avg)
#         writer.add_scalar('SQ_diff_std/val',valid_SQ_diff_std)
#     logging("Epoch [%d]/[%d] train_loss %.6f valid_loss %.6f valid_CQ_diff_avg %.6f valid_CQ_diff_std %.6f valid_SQ_diff_avg %.6fvalid_SQ_diff_std %.6f duration : %.4f"%
#         (epoch,n_epoch,train_loss,val_loss,valid_CQ_diff_avg,valid_CQ_diff_std,valid_SQ_diff_avg,valid_SQ_diff_std,time.time()-st))
    logging("Epoch [%d]/[%d] train_loss %.6f valid_loss %.6f train_L2_loss %.6f valid_L2_loss %.6f train_content_loss %.6f valid_content_loss %.6f train_style_loss %.6f valid_style_loss %.6f duration : %.4f"%(epoch,n_epoch,train_loss,val_loss,train_L2_loss,val_L2_loss,train_content_loss,val_content_loss, train_style_loss, valid_style_loss, time.time()-st))    
if verbose and not is_test:
#     log.close()
    writer.close()
logging("[!] training end")

