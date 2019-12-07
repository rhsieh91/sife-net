import os
import pdb
import torch
import torchvision
import numpy as np
import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as F

from pytorch_i3d import InceptionI3d
from pytorch_sife import SIFE
from torch.optim import lr_scheduler
from torch.utils.data import DataLoader
from torchvision.transforms import Compose, ToTensor, Resize
from torch.utils.tensorboard import SummaryWriter

from data_loader_jpeg import *

import sys
import argparse
parser = argparse.ArgumentParser()
parser.add_argument('--lr', type=float, help='learning rate')
parser.add_argument('--bs', type=int, help='batch size')
parser.add_argument('--epochs', type=int, help='number of epochs')
args = parser.parse_args()


def train(model, optimizer, train_loader, test_loader, epochs, 
          save_dir='', use_gpu=False, lr_sched=None):
    # Enable GPU if available
    if use_gpu and torch.cuda.is_available():
        device = torch.device('cuda')
    else:
        device = torch.device('cpu')
    print('Using device:', device)
    model = model.to(device=device) # move model parameters to CPU/GPU
    
    writer = SummaryWriter() # Tensorboard logging
    dataloaders = {'train': train_loader, 'val': test_loader}   
    best_train_action = -1 # keep track of best val accuracy seen so far
    best_val_action = -1 # keep track of best val accuracy seen so far
    best_train_scene = -1 
    best_val_scene = -1 
    n_iter = 0

    # Training loop
    for e in range(epochs):    
        print('Epoch {}/{}'.format(e, epochs))
        print('-'*10)

        for phase in ['train', 'val']:
            if phase == 'train':
                model.train(True)
                print('-'*10, 'TRAINING', '-'*10)
            else:
                model.train(False)  # set model to eval mode
                print('-'*10, 'VALIDATION', '-'*10)

            num_correct_actions = 0 # keep track of number of correct predictions
            num_correct_scenes = 0

            # Iterate over data
            for data in dataloaders[phase]:
                inputs = data[0] # shape = B x C x T x H x W
                inputs = inputs.to(device=device, dtype=torch.float32) # model expects inputs of float32

                # Forward pass
                if phase == 'train':
                    per_frame_logits, scene_logits = model(inputs)
                else: 
                    with torch.no_grad(): # disable autograd to reduce memory usage
                        per_frame_logits, scene_logits = model(inputs)

                # Due to the strides and max-pooling in I3D, it temporally downsamples the video by a factor of 8
                # so we need to upsample (F.interpolate) to get per-frame predictions
                # ALTERNATIVE: Take the average to get per-clip prediction
                per_frame_logits = F.interpolate(per_frame_logits, size=inputs.shape[2], mode='linear') # output shape = B x NUM_CLASSES x T

                # Average across frames to get a single prediction per clip
                mean_frame_logits = torch.mean(per_frame_logits, dim=2) # shape = B x NUM_CLASSES
                mean_frame_logits = mean_frame_logits.to(device=device) # might already be loaded in CUDA but adding this line just in case
                _, pred_action_idx = torch.max(mean_frame_logits, dim=1) # shape = B, values are indices
                _, pred_scene_idx = torch.max(scene_logits, dim=1)

                # Ground truth labels
                action_idx = data[1] # shape = B
                action_idx = action_idx.to(device=device)
                num_correct_actions += torch.sum(pred_action_idx == action_idx)

                scene_idx = torch.ones_like(pred_scene_idx) # TODO: This is just to test the code, still need actual scene ground truth labels
                scene_idx = scene_idx.to(device=device)
                num_correct_scenes += torch.sum(pred_scene_idx == scene_idx)

                # Backward pass only if in 'train' mode
                if phase == 'train':
                    # Compute combined action and scene classification loss
                    action_loss = F.cross_entropy(mean_frame_logits, action_idx)
                    scene_loss = F.cross_entropy(scene_logits, scene_idx)
                    loss = action_loss + scene_loss

                    writer.add_scalar('Loss/train_action', action_loss, n_iter)
                    writer.add_scalar('Loss/train_scene', scene_loss, n_iter)
                    writer.add_scalar('Loss/train_total', loss, n_iter)
                    
                    optimizer.zero_grad()
                    loss.backward() 
                    optimizer.step()

                    if n_iter % 10 == 0:
                        print('{}, action_loss = {}'.format(phase, action_loss))
                        print('{}, scene_loss = {}'.format(phase, scene_loss))
                        print('{}, total_loss = {}'.format(phase, loss))

                    n_iter += 1

            # Log train/val accuracy
            action_accuracy = float(num_correct_actions) / len(dataloaders[phase].dataset)
            print('num_correct_actions = {}'.format(num_correct_actions))
            print('size of dataset = {}'.format(len(dataloaders[phase].dataset)))
            print('{}, action accuracy = {}'.format(phase, action_accuracy))

            scene_accuracy = float(num_correct_scenes) / len(dataloaders[phase].dataset)
            print('num_correct_scenes = {}'.format(num_correct_scenes))
            print('size of dataset = {}'.format(len(dataloaders[phase].dataset)))
            print('{}, scene accuracy = {}'.format(phase, scene_accuracy))

            if phase == 'train':
                writer.add_scalar('Accuracy/train_action', action_accuracy, e)
                if action_accuracy > best_train_action:
                    best_train_action = action_accuracy
                    print('BEST ACTION TRAINING ACCURACY: {}'.format(best_train_action))
                    save_checkpoint(model, optimizer, loss, save_dir, e, n_iter) # TODO: Determine which checkpoint to save
                writer.add_scalar('Accuracy/train_scene', scene_accuracy, e)
                if scene_accuracy > best_train_scene:
                    best_train_scene = action_accuracy
                    print('BEST SCENE TRAINING ACCURACY: {}'.format(best_train_scene))
                    save_checkpoint(model, optimizer, loss, save_dir, e, n_iter)
            else:
                writer.add_scalar('Accuracy/val_action', action_accuracy, e)
                if action_accuracy > best_val_action:
                    best_val_action = action_accuracy
                    print('BEST ACTION VALIDATION ACCURACY: {}'.format(best_val_action))
                    save_checkpoint(model, optimizer, loss, save_dir, e, n_iter)
                writer.add_scalar('Accuracy/val_scene', scene_accuracy, e)
                if scene_accuracy > best_val_scene:
                    best_val_scene = action_accuracy
                    print('BEST SCENE VALIDATION ACCURACY: {}'.format(best_val_scene))
                    save_checkpoint(model, optimizer, loss, save_dir, e, n_iter)

        if lr_sched is not None:
            lr_sched.step() # decay learning rate according 

    writer.close()  


def save_checkpoint(model, optimizer, loss, save_dir, epoch, n_iter):
    """Saves checkpoint of model weights during training."""
    if not os.path.exists(save_dir):
        os.makedirs(save_dir)

    save_path = save_dir + str(epoch).zfill(2) + str(n_iter).zfill(6) + '.pt'
    torch.save({
                'epoch': epoch,
                'model_state_dict': model.state_dict(),
                'optimizer_state_dict': optimizer.state_dict(),
                'loss': loss
                },
                save_path)


if __name__ == '__main__':
    if len(sys.argv) < 4:
      parser.print_usage()
      sys.exit(1)

    # Hyperparameters
    USE_GPU = True
    NUM_ACTIONS = 27 
    NUM_SCENES = 10
    LR = args.lr
    BATCH_SIZE = args.bs
    EPOCHS = args.epochs 
    SAVE_DIR = 'checkpoints_lr' + str(args.lr) + '_bs' + str(args.bs) + '/'
    NUM_WORKERS = 2
    SHUFFLE = True
    PIN_MEMORY = True
    
    print('LR =', LR)
    print('BATCH_SIZE =', BATCH_SIZE)
    print('EPOCHS =', EPOCHS)
    print('SAVE_DIR =', SAVE_DIR)

    # Transforms
    SPATIAL_TRANSFORM = Compose([
        Resize((224, 224)),
        ToTensor()
        ])

    # Load dataset
    d_train = VideoFolder(root="/vision/group/video/scratch/jester/rgb",
                          csv_file_input="/vision/group/video/scratch/jester/annotations/jester-v1-train.csv",
                          csv_file_labels="/vision/group/video/scratch/jester/annotations/jester-v1-labels.csv",
                          clip_size=16,
                          nclips=1,
                          step_size=1,
                          is_val=False,
                          transform=SPATIAL_TRANSFORM,
                          loader=default_loader)

    print('Size of training set = {}'.format(len(d_train)))
    train_loader = DataLoader(d_train, 
                              batch_size=BATCH_SIZE,
                              shuffle=SHUFFLE, 
                              num_workers=NUM_WORKERS,
                              pin_memory=PIN_MEMORY)

    d_val = VideoFolder(root="/vision/group/video/scratch/jester/rgb",
                        csv_file_input="/vision/group/video/scratch/jester/annotations/jester-v1-validation.csv",
                        csv_file_labels="/vision/group/video/scratch/jester/annotations/jester-v1-labels.csv",
                        clip_size=16,
                        nclips=1,
                        step_size=1,
                        is_val=True,
                        transform=SPATIAL_TRANSFORM,
                        loader=default_loader)

    print('Size of validation set = {}'.format(len(d_val)))
    val_loader = DataLoader(d_val, 
                            batch_size=BATCH_SIZE,
                            shuffle=SHUFFLE, 
                            num_workers=NUM_WORKERS,
                            pin_memory=PIN_MEMORY)
    
    # Load pre-trained I3D backbone and set up SIFE
    i3d = InceptionI3d(400, in_channels=3) # pre-trained model has 400 output classes
    i3d.load_state_dict(torch.load('models/rgb_imagenet.pt'))
    sife = SIFE(backbone=i3d, num_features=1024, num_actions=NUM_ACTIONS, num_scenes=NUM_SCENES)

    # Set up optimizer and learning rate schedule
    optimizer = optim.Adam(i3d.parameters(), lr=LR) 
    lr_sched = optim.lr_scheduler.MultiStepLR(optimizer, [10, 20], gamma=0.1) # decay learning rate by gamma at epoch 10 and 20

    # Start training
    train(sife, optimizer, train_loader, val_loader, epochs=EPOCHS, 
          save_dir=SAVE_DIR, use_gpu=USE_GPU, lr_sched=lr_sched)
