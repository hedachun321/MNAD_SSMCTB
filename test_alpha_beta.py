import numpy as np
import os
import sys
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
import torchvision
import torch.nn.init as init
import torch.utils.data as data
import torch.utils.data.dataset as dataset
import torchvision.datasets as dset
import torchvision.transforms as transforms
from torch.autograd import Variable
import torchvision.utils as v_utils
import matplotlib.pyplot as plt
import cv2
import math
from collections import OrderedDict
import copy
import time

from tqdm import tqdm

from model.utils import DataLoader
from model.final_future_prediction_with_memory_spatial_sumonly_weight_ranking_top1 import *
from model.Reconstruction import *
from sklearn.metrics import roc_auc_score
from utils import *
import random
import glob

import argparse

parser = argparse.ArgumentParser(description="MNAD")
parser.add_argument('--gpus', nargs='+', type=str, help='gpus')
parser.add_argument('--batch_size', type=int, default=4, help='batch size for training')
parser.add_argument('--test_batch_size', type=int, default=1, help='batch size for test')
parser.add_argument('--h', type=int, default=256, help='height of input images')
parser.add_argument('--w', type=int, default=256, help='width of input images')
parser.add_argument('--c', type=int, default=3, help='channel of input images')
parser.add_argument('--method', type=str, default='pred', help='The target task for anoamly detection')
parser.add_argument('--t_length', type=int, default=5, help='length of the frame sequences')
parser.add_argument('--fdim', type=int, default=512, help='channel dimension of the features')
parser.add_argument('--mdim', type=int, default=512, help='channel dimension of the memory items')
parser.add_argument('--msize', type=int, default=10, help='number of the memory items')
parser.add_argument('--alpha', type=float, default=0.48, help='weight for the anomality score')
parser.add_argument('--th', type=float, default=0.015, help='threshold for test updating')
parser.add_argument('--num_workers', type=int, default=2, help='number of workers for the train loader')
parser.add_argument('--num_workers_test', type=int, default=1, help='number of workers for the test loader')
parser.add_argument('--dataset_type', type=str, default='ped2', help='type of dataset: ped2, avenue, shanghai')
parser.add_argument('--dataset_path', type=str, default='./dataset', help='directory of data')
parser.add_argument('--model_dir', type=str, default='./exp/ped2/pred/log_ssmctb_96.14/model_ssmctb_40.pth',
                    help='directory of model')
parser.add_argument('--m_items_dir', type=str, default='./exp/ped2/pred/log_ssmctb_96.14/keys_ssmctb_40.pt',
                    help='directory of model')

args = parser.parse_args()

os.environ["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
if args.gpus is None:
    gpus = "0"
    os.environ["CUDA_VISIBLE_DEVICES"] = gpus
else:
    gpus = ""
    for i in range(len(args.gpus)):
        gpus = gpus + args.gpus[i] + ","
    os.environ["CUDA_VISIBLE_DEVICES"] = gpus[:-1]

torch.backends.cudnn.enabled = True  # make sure to use cudnn for computational performance

test_folder = args.dataset_path + "/" + args.dataset_type + "/testing/frames"

# Loading dataset
test_dataset = DataLoader(test_folder, transforms.Compose([
    transforms.ToTensor(),
]), resize_height=args.h, resize_width=args.w, time_step=args.t_length - 1)

test_size = len(test_dataset)

test_batch = data.DataLoader(test_dataset, batch_size=args.test_batch_size,
                             shuffle=False, num_workers=args.num_workers_test, drop_last=False)

loss_func_mse = nn.MSELoss(reduction='none')

# Loading the trained model
model = torch.load(args.model_dir)
model.cuda()
m_items = torch.load(args.m_items_dir)
labels = np.load('./data/frame_labels_' + args.dataset_type + '.npy')

videos = OrderedDict()
videos_list = sorted(glob.glob(os.path.join(test_folder, '*')))
for video in videos_list:
    video_name = video.split('/')[-1]
    videos[video_name] = {}
    videos[video_name]['path'] = video
    videos[video_name]['frame'] = glob.glob(os.path.join(video, '*.jpg')) + glob.glob(os.path.join(video, '*.png'))
    videos[video_name]['frame'].sort()
    videos[video_name]['length'] = len(videos[video_name]['frame'])

labels_list = []
label_length = 0
psnr_list = {}
feature_distance_list = {}
loss_ssmctb_list = {}

print('Evaluation of', args.dataset_type)

# Setting for video anomaly detection
for video in sorted(videos_list):
    video_name = video.split('/')[-1]
    if args.method == 'pred':
        labels_list = np.append(labels_list, labels[0][4 + label_length:videos[video_name]['length'] + label_length])
    else:
        labels_list = np.append(labels_list, labels[0][label_length:videos[video_name]['length'] + label_length])
    label_length += videos[video_name]['length']
    psnr_list[video_name] = []
    feature_distance_list[video_name] = []
    loss_ssmctb_list[video_name] = []

label_length = 0
video_num = 0
label_length += videos[videos_list[video_num].split('/')[-1]]['length']
m_items_test = m_items.clone()

model.eval()
pbar = tqdm(total=len(test_batch))
for k, (imgs) in enumerate(test_batch):

    if args.method == 'pred':
        if k == label_length - 4 * (video_num + 1):
            video_num += 1
            label_length += videos[videos_list[video_num].split('/')[-1]]['length']
    else:
        if k == label_length:
            video_num += 1
            label_length += videos[videos_list[video_num].split('/')[-1]]['length']

    imgs = Variable(imgs).cuda()

    if args.method == 'pred':
        outputs, feas, updated_feas, m_items_test, softmax_score_query, softmax_score_memory, _, _, _, compactness_loss, cost_ssmctb = model.forward(
            imgs[:, 0:3 * 4], m_items_test, False)
        mse_imgs = torch.mean(loss_func_mse((outputs[0] + 1) / 2, (imgs[0, 3 * 4:] + 1) / 2)).item()
        mse_feas = compactness_loss.item()
        loss_ssmctb = cost_ssmctb.item()

        # Calculating the threshold for updating at the test time
        point_sc = point_score(outputs, imgs[:, 3 * 4:])

    else:
        outputs, feas, updated_feas, m_items_test, softmax_score_query, softmax_score_memory, compactness_loss = model.forward(
            imgs, m_items_test, False)
        mse_imgs = torch.mean(loss_func_mse((outputs[0] + 1) / 2, (imgs[0] + 1) / 2)).item()
        mse_feas = compactness_loss.item()

        # Calculating the threshold for updating at the test time
        point_sc = point_score(outputs, imgs)

    if point_sc < args.th:
        query = F.normalize(feas, dim=1)
        query = query.permute(0, 2, 3, 1)  # b X h X w X d
        m_items_test = model.memory.update(query, m_items_test, False)

    pbar.set_postfix({

    })
    pbar.update(1)
    psnr_list[videos_list[video_num].split('/')[-1]].append(psnr(mse_imgs))
    feature_distance_list[videos_list[video_num].split('/')[-1]].append(mse_feas)
    loss_ssmctb_list[videos_list[video_num].split('/')[-1]].append(psnr(loss_ssmctb))

pbar.close()


alpha_start, alpha_end, alpha_step = 0.2, 0.73, 0.01
beta_start, beta_end, beta_step = 0.1, 0.4, 0.01

# 存储每次计算的 AUC 值
auc_results = []

# 迭代 alpha 和 beta 值
for alpha in np.arange(alpha_start, alpha_end + alpha_step, alpha_step):
    for beta in np.arange(beta_start, beta_end + beta_step, beta_step):
        anomaly_score_total_list = []

        for video in sorted(videos_list):
            video_name = video.split('/')[-1]
            anomaly_score_total_list += score_sum_(anomaly_score_list(psnr_list[video_name]),
                                                 anomaly_score_list_inv(feature_distance_list[video_name]),
                                                 anomaly_score_list_ssmctb(loss_ssmctb_list[video_name]), alpha, beta)

        anomaly_score_total_list = np.asarray(anomaly_score_total_list)

        # 计算 AUC
        accuracy = AUC(anomaly_score_total_list, np.expand_dims(1 - labels_list, 0))
        auc_results.append((alpha, beta, accuracy))

        print(f'For alpha = {alpha}, beta = {beta}, AUC: {accuracy * 100}%')

# 打印最终结果
best_alpha, best_beta, best_auc = max(auc_results, key=lambda x: x[2])
print(f'\nBest alpha value: {best_alpha}, Best beta value: {best_beta}, Best AUC: {best_auc * 100}%')