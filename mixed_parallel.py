import argparse
import os, sys
import math
import random
import shutil
import time
import warnings

import torch
import torch.nn as nn
import torch.nn.parallel
import torch.backends.cudnn as cudnn
import torch.distributed as dist
import torch.optim as optim
import torch.utils.data
import torch.utils.data.distributed
from torch.utils.data import Dataset

#import torchvision.transforms as transforms
#import torchvision.datasets as datasets
#import torchvision.models as models

import numpy as np
import scipy as misc

import resnet3d
#from model_3d_mpi import train, eval

import nibabel as nib

from sklearn.metrics import mean_squared_error


import time


best_prec1 = 0

rank = 0
world_size = 0

local_rank = 0
local_size = 0

node_num = 0
node_idx = 0



def avg_grad(model):
    for param in model.parameters():
        dist.all_reduce(param.grad.data, op=dist.ReduceOp.SUM)
        param.grad.data /= float(world_size)


def avg_param(model):
    for param in model.parameters():
        dist.all_reduce(param.data, op=dist.ReduceOp.SUM)
        param.data /= float(world_size)


def reduce_loss(total_loss, n_samples):
    reduction = torch.FloatTensor([total_loss,n_samples])
    dist.all_reduce(reduction, op=dist.ReduceOp.SUM)
    if rank==0: print('n_samples : ', int(reduction[1].item()))
    return float(reduction[0].item() / reduction[1].item())




class MRIDataset(Dataset):
    def __init__(self, input_data, target):
        self.X_data = input_data
        self.Y_data = target

    def __len__(self):
        return len(self.Y_data)

    def __getitem__(self, idx):
#        dim = 120
        x = np.array(self.X_data[idx].dataobj)
#        x = misc.imresize(x, (dim, dim, dim))
#        x = resize(x, (dim, dim, dim), anti_aliasing=True)
#        print('MRI max value is: ', x.max())
#        print('MRI image size dim is:', x.shape)
        return (x, self.Y_data[idx])







def main():


    global rank, world_size, local_rank, local_size, node_num, node_idx, proc_time

    # Parsing arguments
    parser = argparse.ArgumentParser(description='ResNet3D for regression')
    parser.add_argument('--data_dir')
    parser.add_argument('--output_dir')
    parser.add_argument('--epoch', type=int, default=1)
    parser.add_argument('--train_batch_size', type=int, default=2)
    parser.add_argument('--valid_batch_size', type=int, default=4)
    parser.add_argument('--lr', type=float, default=0.01)
    parser.add_argument('--momentum', type=float, default=0.5)
    args = parser.parse_args()


    # get the rank and wsize using pytorch mpi backend
    rank = dist.get_rank()   # rank idx (not by the physical node) 
    world_size = dist.get_world_size()  # total size (across physical node)

    local_rank = (int)(os.environ['SLURM_LOCALID'])             # rank idx within each local_size
    local_size = (int)(os.environ['SLURM_NTASKS_PER_NODE'])   # world_size within each physical node

    node_num = world_size // local_size     # number of nodes (this is actually the physical node... yes, assume we don't know this at the beginning...)
    
    node_idx = rank // local_size # physical node index...


    gpu_per_node = 8 # this is given...


    device_idx_per_local_rank = [(local_rank * gpu_per_node//local_size + i) for i in range(gpu_per_node//local_size)]


    device_list = device_idx_per_local_rank

    
    print('device_idx_per_local_rank is: {}'.format(device_idx_per_local_rank))

    
    print('current rank is {}'.format(rank))
    print('world size is {}'.format(world_size))

    print('local_rank is {}'.format(local_rank))
    print('local_size is {}'.format(local_size))

    
    proc_time = []


    if rank >=  0: ## dummpy if statement

        sys.path.append('/global/homes/y/yanzhang/nesap-lstnet/dataset')
        
        train_img = np.load('train_data_img.npy', allow_pickle=True)
        valid_img = np.load('valid_data_img.npy', allow_pickle=True)
        train_target = np.load('train_data_target.npy', allow_pickle=True)
        valid_target = np.load('valid_data_target.npy', allow_pickle=True)
        print('data loaded!')


        model = resnet3d.PipelinedResNet3dRegressor(device_list)
        
        optimizer = optim.SGD(model.parameters(), lr=args.lr, momentum=args.momentum)

    
        train_dataset = MRIDataset(train_img, train_target)
        train_loader = torch.utils.data.DataLoader(train_dataset, batch_size=args.train_batch_size, shuffle=True)
        valid_dataset = MRIDataset(valid_img, valid_target)
        valid_loader = torch.utils.data.DataLoader(valid_dataset, batch_size=args.valid_batch_size, shuffle=True)


        print('sync starts!')
        avg_param(model)
        print('sync ends!')


        dist.barrier();
        
        print('begin training now!')

        
        #t1 = time.time()
        for i in range(args.epoch):
            train(model, args.epoch, train_loader, valid_loader, optimizer, args.output_dir, device_list)
            
         #   t2 = time.time() - t1
         #   proc_time.append(t2)
        dist.barrier();

        #np.save('proc_time_gpu_wsize_'+ world_size + '_lsize_' + local_size +'.npy', proc_time)
        #eval(model, valid_loader)

        

        
def train(model, epoch, train_loader, valid_loader, optimizer, output_dir, devices):
    model.train()
    loss = nn.L1Loss()

    loss = loss.to('cuda:'+str(devices[-1]))
    best_mse = float('inf')


    t1 = time.time()
    for batch_idx, (batch_img, batch_target) in enumerate(train_loader):
        
        batch_img = batch_img.unsqueeze(1)

        optimizer.zero_grad()


        batch_img = batch_img.to('cuda:'+str(devices[0]))
        batch_target = batch_target.float().to('cuda:'+str(devices[-1]))

        output = model(batch_img)
        res = loss(output.squeeze(), batch_target)
        res.backward()
        optimizer.step()

        
        avg_grad(model)

        t2 = time.time() - t1
        proc_time.append(t2)

        if batch_idx == 9:

            np.save('proc_time_gpu.npy', proc_time)
    
            break
        
        print('Gradient averged for the rank of {}'.format(rank))

 #       target_true = []
#        target_pred = []

    #   if batch_idx % 10 == 0:
            
  #          target_true.append(batch_target.cpu())
  #          for pred in output:
  #              target_pred.append(pred.cpu())

#        mae = res.numpy()
        print('Mean absolute error is: {}'.format(res))
        print('true target is {}'.format(batch_target))
        print('predicted is {}'.format(output))

        
    #    if cur_mse < best_mse:
    #        best_mse = cur_mse
    #print('The best MSE is {}'.format(best_mse))



def eval(model, valid_loader, devices):
    with torch.no_grad():
        model.cpu()
        model.eval()
        loss = nn.L1Loss()

        #loss = loss.to('cuda:'+str(devices[1]))
  
        target_true = []
        target_pred = []

        for batch_idx, (batch_img, batch_target) in enumerate(valid_loader):
            batch_img = batch_img.unsqueeze(1)

            #batch_img = batch_img.to('cuda:'+str(devices[0]))
            #batch_target = batch_target.float().to('cuda:'+str(devices[1]))

            output = model(batch_img)
            res = loss(output.squeeze(), batch_target)

            target_true.extend(batch_target.cpu())
            for pred in output:
                target_pred.extend(pred.cpu())


        mse = mean_squared_error(target_true, target_pred)
        print('Mean squared error: {}'.format(mse))

    return mse











        

            
        

def save_checkpoint(state, is_best, filename='checkpoint.pth.tar'):
    torch.save(state, filename)
    if is_best:
        shutil.copyfile(filename, 'model_best.pth.tar')


class AverageMeter(object):
    """Computes and stores the average and current value"""
    def __init__(self):
        self.reset()

    def reset(self):
        self.val = 0
        self.avg = 0
        self.sum = 0
        self.count = 0

    def update(self, val, n=1):
        self.val = val
        self.sum += val * n
        self.count += n
        self.avg = self.sum / self.count


def warmup_learning_rate(optimizer, loader_len, epoch, it):
    base_lr = 0.05
    end_ep = 5

    if epoch < end_ep and args.lr > base_lr :
        total_grid = loader_len*end_ep
        lr = base_lr + ((it + loader_len*epoch)/float(total_grid))*(args.lr-base_lr)
        
        #print('warmup_learning_rate() : i='+str(it)+', lr=', lr)

        for param_group in optimizer.param_groups:
            param_group['lr'] = lr



def adjust_learning_rate(optimizer, epoch, power):
    """Sets the learning rate to the initial LR decayed by 10 every 30 epochs"""
    lr = args.lr * (0.1 ** (power*(epoch // 30)))
    for param_group in optimizer.param_groups:
        param_group['lr'] = lr


def accuracy(output, target, topk=(1,)):
    """Computes the precision@k for the specified values of k"""
    with torch.no_grad():
        maxk = max(topk)
        batch_size = target.size(0)

        _, pred = output.topk(maxk, 1, True, True)
        pred = pred.t()
        correct = pred.eq(target.view(1, -1).expand_as(pred))

        res = []
        for k in topk:
            correct_k = correct[:k].view(-1).float().sum(0, keepdim=True)
            res.append(correct_k.mul_(100.0 / batch_size))
        return res


if __name__ == '__main__':

  
    dist.init_process_group('mpi')

    main()
