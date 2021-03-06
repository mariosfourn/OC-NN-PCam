from __future__ import print_function
from MyModels import AutoEncoder
from PIL import Image
from scipy.ndimage.interpolation import rotate
from tensorboardX import SummaryWriter
from torch.utils.data import Dataset, DataLoader
from torchvision import datasets, transforms

import argparse
import itertools
import matplotlib.pyplot as plt
import numpy as np
import os
import pandas as pd
import pytorch_ssim
import random
import sys
import time
import h5py
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
import torchvision
import torchvision.models as models
import itertools
from sklearn.metrics import roc_curve, auc
from tqdm import tqdm


class TrainingDataset(Dataset):
    """Training set with only normal images """

    def __init__(self, filename, transform=None):
        """
        Args:
            filename: input h5 filename
        """
        h5_file = h5py.File(filename, 'r')
        self.dataset = h5_file['x']
        self.transform = transform

    def __len__(self):
        return self.dataset.shape[0]

    def __getitem__(self, idx):
        image = self.dataset[idx]

        if self.transform is not None:
            image = self.transform(image)

        return image


class ValidationDataset(Dataset):
    """ Validation set with both normal and abnormal images"""

    def __init__(self, images_filename, labels_filename, transform=None):
        """
        Args:
            image_filename: h5  image filename
            labels_filename: h5 labels filename
        """
        h5_images = h5py.File(images_filename, 'r')
        h5_labels = h5py.File(labels_filename, 'r')
        self.dataset = h5_images['x']
        self.labels = h5_labels['y']
        self.transform = transform

    def __len__(self):
        return self.dataset.shape[0]

    def __getitem__(self, idx):
        image = self.dataset[idx]

        if self.transform is not None:
            image = self.transform(image)
        label = int(self.labels[idx][:])

        return (image, label)

def ocnn_objective(X, nu, w1, w2, R):
    #learning object - loss function
    W = w1
    V = w2

    term1 = 0.5 * (W.norm(2))**2
    term2 = 0.5 * (V.norm(2))**2
    term3 = 1 / nu * F.relu_(R - nnScore(X, W, V)).mean()
    term4 = -R

    return term1 + term2 + term3 + term4

def nnScore(X, W, V):
    #Score function
    return torch.matmul(torch.sigmoid(torch.matmul(X, W)), V)


def evaluate_training_score(model, dataloader, w1, w2, r):
    #Evaluate score on a subset of the training data
    #It returns the mean and std of the training scores

    model.eval()
    results = []

    with torch.no_grad():
        for batch_idx, data in enumerate(dataloader):

            #Get feature vector from pre-trained encoder
            feature_vector = model.encoder(data)  #[N,512,1,1]

            # Reshape
            feature_vector = feature_vector.view(-1, 512)  # [N,512]

            # Get Score
            score = nnScore(feature_vector, w1, w2).numpy().flatten() - r
            results.append(score.tolist())

            #Break after 10 batches

            if batch_idx % 10 == 0 and batch_idx >0 : break

    # Flatten list
    results = list(itertools.chain(*results))
    results = np.array(results)

    return results.mean(), results.std()


def validation_scores(model, dataloader, w1, w2, r):
    #Get mean score for normal and abnormal validation samples
    #It returns the mean of normal and abnormal

    model.eval()
    normal_results = []
    abnormal_results = []

    with torch.no_grad():

        for batch_idx, (data, label) in enumerate(dataloader):

            #Get feature vector from pre-trained encoder
            feature_vector = model.encoder(data)  #[N,512,1,1]

            # Reshape
            feature_vector = feature_vector.view(-1, 512)  # [N,512]

            # Get the scores
            score = nnScore(feature_vector, w1, w2).numpy().flatten() - r

            index= label.numpy()

            normal_results.append(score[index<1].tolist())
            abnormal_results.append(score[index>0].tolist())
    
    #Combine all results in one list and return the mean 
    normal_results = list(itertools.chain(*normal_results))
    abnormal_results = list(itertools.chain(*abnormal_results)) 

    normal_results = np.array(normal_results)
    abnormal_results = np.array(abnormal_results)

    return normal_results.mean(), abnormal_results.mean()


def validation_roc(model, dataloader, w1, w2, r, path, epoch):
    #Plot ROC curve for validation set

    model.eval()
    scores = []
    labels = []

    with torch.no_grad():
        for batch_idx, (data, label) in enumerate(dataloader):

            #Get feature vector from pre-trained encoder
            feature_vector = model.encoder(data)  #[N,512,1,1]

            # Reshape
            feature_vector = feature_vector.view(-1, 512)  # [N,512]

            #Get scores
            score = nnScore(feature_vector, w1, w2).numpy().flatten() - r

            scores.append(score.tolist())

            labels.append(label.numpy().flatten().tolist())

    scores= list(itertools.chain(*scores))
    labels = list (itertools.chain(*labels))

    # Get ROC Curve
    fpr, tpr, thr = roc_curve(labels, scores, pos_label=1)
    # Get ROC AUC
    roc_auc = auc(fpr, tpr)

    #Plot ROC figure

    plt.figure()
    lw = 2

    plt.plot(
        fpr,
        tpr,
        color='darkorange',
        lw=lw,
        label='ROC curve (area = %0.2f)' % roc_auc)

    plt.plot([0, 1], [0, 1], color='navy', lw=lw, linestyle='--')
    plt.xlim([0.0, 1.0])
    plt.ylim([0.0, 1.05])
    plt.xlabel('False Positive Rate')
    plt.ylabel('True Positive Rate')
    plt.title('Receiver operating characteristic example')
    plt.legend(loc="lower right")
    output_name = os.path.join(path,
                               'Validation_ROC_epoch_{:03d}.png'.format(epoch))
    plt.savefig(output_name)

    return roc_auc



def createDalaLoaders(args,path):
    #Define transformation for training set
    training_transformations = transforms.Compose([
        transforms.ToPILImage(),
        transforms.Pad(96, padding_mode='reflect'),
        transforms.RandomRotation(30),
        transforms.CenterCrop(96),
        transforms.ToTensor()
    ])

    #Training dataset directory
    train_dir = os.path.join(
        path, 'camelyonpatch_level_2_split_train_normal_subsample.h5')

    # Define train loader 
    train_loader = DataLoader(
        TrainingDataset(train_dir, transform=training_transformations),
        batch_size=args.batch_size,
        shuffle=True)

    # validation sets root dir
    valid_x = os.path.join(path, 'camelyonpatch_level_2_split_valid_x_subsample.h5')
    valid_y = os.path.join(path, 'camelyonpatch_level_2_split_valid_y_subsample.h5')

    # Define validation dataloader

    valid_loader = DataLoader(
        ValidationDataset(valid_x, valid_y, transform=transforms.ToTensor()),
        batch_size=args.eval_batch_size,
        shuffle=False)

    return train_loader, valid_loader


def main():
    parser = argparse.ArgumentParser(
        description='OC-NN With Encoder for feature extraction')
    parser.add_argument(
        '--batch-size',
        type=int,
        default=128,
        metavar='N',
        help='input batch size for training (default: 128)')

    parser.add_argument(
        '--eval-batch-size',
        type=int,
        default=200,
        metavar='N',
        help='batch size for evaluation phase (default: 200)')

    parser.add_argument(
        '--epochs',
        type=int,
        default=100,
        help='number of training epochs (Default=100)')

    parser.add_argument(
        '--lr',
        type=float,
        default=0.0001,
        help='learning rate (Default=0.0001)')

    parser.add_argument(
        '--r',
        type=float,
        default=0.1,
        help='bias starting value (Default=0.1)')

    parser.add_argument(
        '--hidden',
        type=int,
        default=256,
        help='number of hidden neurons (hidden=32)')

    parser.add_argument(
        '--nu', type=float, default=0.04, help='quantile (Default=0.04)')

    parser.add_argument(
        '--momentum',
        type=float,
        default=0.9,
        help='momentum for SGD optimiser (default=0.9)')

    parser.add_argument(
        '--log-progress',
        type=int,
        default=10,
        help='log progress every this number of batches (default = 10)')

    parser.add_argument(
        '--name', type=str, default='test', help='name of run (default=test')

    args = parser.parse_args()

    # Set manual seed
    RANDOM_SEED = 42
    torch.manual_seed(RANDOM_SEED)

    #Print arguments
    for arg in vars(args):
        sys.stdout.write('{} = {} \n'.format(arg, getattr(args, arg)))
        sys.stdout.flush()
    sys.stdout.write('Random torch seed:{}\n'.format(torch.initial_seed()))
    sys.stdout.flush()

    #Load pre-trained encoder
    encoder_weights_roodir = './model_run_1621_lr=1e-4_dropout=0/checkpoint_epoch_50.pt'

    # Create model
    autoencoder = AutoEncoder(0)

    #Load weights
    autoencoder.load_state_dict(torch.load(encoder_weights_roodir))

    #Freeze weights during training
    for param in autoencoder.parameters():
        param.requires_grad = False

    #Set up trainable weights
    w1 = torch.empty(512, args.hidden, dtype=torch.float32, requires_grad=True)
    w2 = torch.empty(args.hidden, 1, dtype=torch.float32, requires_grad=True)

    # Initialise weights with Xavier initialisation
    nn.init.xavier_normal_(w1, gain=1)
    nn.init.xavier_normal_(w2, gain=1)

    optimizer = optim.SGD([w1, w2], lr=args.lr, momentum=args.momentum)

    #Get DataLoaders:
    train_loader, valid_loader  = createDalaLoaders(args,'./pcamv1')

    #Create output/logging file
    logging_dir = './logs_' + args.name
    if not os.path.exists(logging_dir):
        os.makedirs(logging_dir)

    #Create TensorboardX summary
    writer = SummaryWriter(logging_dir, comment='One Class Classifier with NN')

    #Start training
    sys.stdout.write('Start training\n')
    sys.stdout.flush()
    n_iter = 0
    r_scalar = args.r

    #Train

    for epoch in range(1, args.epochs + 1):
        sys.stdout.write('Starting epoch {}/{} \n '.format(epoch, args.epochs))
        sys.stdout.flush()

        for batch_idx, data in enumerate(train_loader):
            autoencoder.eval()

            # Get feature vector from pre-trained encoder
            feature_vector = autoencoder.encoder(data)  #[N,512,1,1]

            # Reshape
            feature_vector = feature_vector.view(-1, 512)  # [N,512]

            # Set optimiser gradient to zero
            optimizer.zero_grad()

            # Get loss
            loss = ocnn_objective(feature_vector, args.nu, w1, w2, r_scalar)

            loss.backward()
            #Step1 of optimisation: Optimise w,V parameters

            optimizer.step()

            # Step2 of optimisation: Optimise bias

            train_score = nnScore(feature_vector, w1, w2)
            r_scalar = float(
                np.percentile(train_score.detach().numpy(), q=100 * args.nu))

            sys.stdout.write(
                'Train Epoch: {} [{}/{} ({:.0f}%)]\tLoss: {:.6f} , r={:.4f}\r'
                .format(epoch, batch_idx * len(data),
                        len(train_loader.dataset),
                        100. * batch_idx / len(train_loader), loss.item(),
                        r_scalar))
            sys.stdout.flush()

            # Log results 
            if batch_idx % args.log_progress == 0 :

                #Log mini-batch loss
                writer.add_scalar('Mini-Batch-loss', loss.item(), n_iter)

                # evaluate score on training data and validation data
                mean, std = evaluate_training_score(autoencoder, train_loader,
                                                     w1, w2, r_scalar)

                normal_mean, abnormal_mean = validation_scores(
                    autoencoder, valid_loader, w1, w2, r_scalar)

                writer.add_scalars(
                    'Scores', {
                        'Train Score Mean': mean,
                        'Train Score Std': std,
                        'Valid Normal': normal_mean,
                        'Valid Abnormal': abnormal_mean
                    }, n_iter)
              
            n_iter += 1


        #Print ROC Curve
        roc_auc= validation_roc(autoencoder, valid_loader, w1, w2, r_scalar, logging_dir, epoch)

        #Log AUC results per epoch
        writer.add_scalar('ROC AUC',roc_auc, epoch)


if __name__ == '__main__':
    main()


