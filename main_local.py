#!/usr/bin/env python
# -*- coding: utf-8 -*-
# Python version: 3.6

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import copy
import numpy as np
from torchvision import datasets, transforms
import torch
import torch.nn
import torch.nn.functional as F

from utils.sampling import mnist_iid, mnist_noniid, cifar_iid, cifar_noniid
from utils.options import args_parser
from models.Update import LocalUpdate
from models.Nets import MLP, CNNMnist, CNNCifar
from models.Fed import FedAvg
from models.test import test_img
from utils.util import setup_seed
from datetime import datetime
from torch.utils.tensorboard import SummaryWriter
from torch.utils.data import DataLoader
import torch.optim as optim
from models.Update import DatasetSplit
import random


def test(model, data_source):
    model.eval()
    total_loss = 0.0
    correct = 0.0
    correct_class = np.zeros(10)
    correct_class_acc = np.zeros(10)
    correct_class_size = np.zeros(10)

    dataset_size = len(data_source.dataset)
    data_iterator = data_source
    with torch.no_grad():
        for batch_id, (data, targets) in enumerate(data_iterator):
            data, targets = data.to(args.device), targets.to(args.device)
            output = model(data)
            total_loss += F.cross_entropy(output, targets,
                                              reduction='sum').item() # sum up batch loss
            pred = output.data.max(1)[1]  # get the index of the max log-probability
            correct += pred.eq(targets.data.view_as(pred)).cpu().sum().item()
            for i in range(10):
                class_ind = targets.data.view_as(pred).eq(i*torch.ones_like(pred))
                correct_class_size[i] += class_ind.cpu().sum().item()
                correct_class[i] += (pred.eq(targets.data.view_as(pred))*class_ind).cpu().sum().item()

        acc = 100.0 * (float(correct) / float(dataset_size))
        for i in range(10):
            correct_class_acc[i] = (float(correct_class[i]) / float(correct_class_size[i]))
        total_l = total_loss / dataset_size
        # print(f'Average loss: {total_l}, Accuracy: {correct}/{dataset_size} ({acc}%)')
        return total_l, acc, correct_class_acc


if __name__ == '__main__':
    # parse args
    args = args_parser()
    args.device = torch.device('cuda:{}'.format(args.gpu) if torch.cuda.is_available() and args.gpu != -1 else 'cpu')
    setup_seed(args.seed)

    # log
    current_time = datetime.now().strftime('%b.%d_%H.%M.%S')
    TAG = 'local_{}_{}_{}_C{}_iid{}_{}'.format(args.dataset, args.model, args.epochs, args.frac, args.iid, current_time)
    logdir = f'runs/{TAG}' if not args.debug else f'/tmp/runs/{TAG}'
    writer = SummaryWriter(logdir)

    # load dataset and split users
    if args.dataset == 'mnist':
        trans_mnist = transforms.Compose([transforms.ToTensor(), transforms.Normalize((0.1307,), (0.3081,))])
        dataset_train = datasets.MNIST('../data/mnist/', train=True, download=True, transform=trans_mnist)
        dataset_test = datasets.MNIST('../data/mnist/', train=False, download=True, transform=trans_mnist)
        # sample users
        if args.iid:
            dict_users = mnist_iid(dataset_train, args.num_users)
        else:
            dict_users = mnist_noniid(dataset_train, args.num_users)

    elif args.dataset == 'cifar':
        # trans_cifar = transforms.Compose([transforms.ToTensor(), transforms.Normalize((0.5, 0.5, 0.5), (0.5, 0.5, 0.5))])
        transform_train = transforms.Compose([
            transforms.RandomCrop(32, padding=4),
            transforms.RandomHorizontalFlip(),
            transforms.ToTensor(),
            transforms.Normalize((0.4914, 0.4822, 0.4465), (0.2023, 0.1994, 0.2010)),
        ])
        transform_test = transforms.Compose([
            transforms.ToTensor(),
            transforms.Normalize((0.4914, 0.4822, 0.4465), (0.2023, 0.1994, 0.2010)),
        ])
        dataset_train = datasets.CIFAR10('../data/cifar', train=True, download=True, transform=transform_train)
        dataset_test = datasets.CIFAR10('../data/cifar', train=False, download=True, transform=transform_test)

        if args.iid:
            dict_users = cifar_iid(dataset_train, args.num_users)
        else:
            dict_users, _ = cifar_noniid(dataset_train, args.num_users, 0.9)
            for k, v in dict_users.items():
                writer.add_histogram(f'user_{k}/data_distribution',
                                     np.array(dataset_train.targets)[v],
                                     bins=np.arange(11))
                writer.add_histogram(f'all_user/data_distribution',
                                     np.array(dataset_train.targets)[v],
                                     bins=np.arange(11), global_step=k)
    else:
        exit('Error: unrecognized dataset')

    test_loader = DataLoader(dataset_test, batch_size=1000, shuffle=False)
    img_size = dataset_train[0][0].shape

    # build model
    if args.model == 'cnn' and args.dataset == 'cifar':
        net_glob = CNNCifar(args=args).to(args.device)
    elif args.model == 'cnn' and args.dataset == 'mnist':
        net_glob = CNNMnist(args=args).to(args.device)
    elif args.model == 'mlp':
        len_in = 1
        for x in img_size:
            len_in *= x
        net_glob = MLP(dim_in=len_in, dim_hidden=200, dim_out=args.num_classes).to(args.device)
    else:
        exit('Error: unrecognized model')
    print(net_glob)
    net_glob.train()

    # copy weights
    w_init = copy.deepcopy(net_glob.state_dict())

    local_acc_final = []
    total_acc_final = []
    # training
    for idx in range(args.num_users):
        print(w_init)
        net_glob.load_state_dict(w_init)
        optimizer = optim.Adam(net_glob.parameters())
        train_loader = DataLoader(DatasetSplit(dataset_train, dict_users[idx]), batch_size=64, shuffle=True)
        image_trainset_weight = np.zeros(10)
        for label in np.array(dataset_train.targets)[dict_users[idx]]:
            image_trainset_weight[label] += 1
        image_trainset_weight = image_trainset_weight / image_trainset_weight.sum()
        list_loss = []
        net_glob.train()
        for epoch in range(args.epochs):
            batch_loss = []
            for batch_idx, (data, target) in enumerate(train_loader):
                data, target = data.to(args.device), target.to(args.device)
                optimizer.zero_grad()
                output = net_glob(data)
                loss = F.cross_entropy(output, target)
                loss.backward()
                optimizer.step()
                if batch_idx % 3 == 0:
                    print('Train Epoch: {} [{}/{} ({:.0f}%)]\tLoss: {:.6f}'.format(
                        epoch, batch_idx * len(data), len(train_loader.dataset),
                               100. * batch_idx / len(train_loader), loss.item()))
                batch_loss.append(loss.item())
            loss_avg = sum(batch_loss)/len(batch_loss)
            print('\nLocal Train loss:', loss_avg)
            list_loss.append(loss_avg)
            writer.add_scalar(f'user{idx}/local_train_loss', loss_avg, epoch)
            total_test_loss, total_test_acc, correct_class_acc = test(net_glob, test_loader)
            local_test_acc = (correct_class_acc * image_trainset_weight).sum() * 100
            writer.add_scalar(f'user{idx}/total_test_acc', total_test_acc, epoch)
            writer.add_scalar(f'user{idx}/local_test_acc', local_test_acc, epoch)
            print('Global Test ACC:', total_test_acc)
            print('Local Test ACC:', local_test_acc)
        local_acc_final.append(local_test_acc)
        total_acc_final.append(total_test_acc)

    # plot loss curve
    plt.figure()
    plt.title('local train acc', fontsize=20)  # 标题，并设定字号大小
    labels = ['local', 'total']
    plt.boxplot([local_acc_final, total_acc_final], labels=labels, notch=True, showmeans=True)
    plt.ylabel('test acc')
    plt.savefig(f'{logdir}/local_train_acc.png')

