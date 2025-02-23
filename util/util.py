import os
import numpy as np
from PIL import Image

import torch
from torch import nn
from torch.nn.modules.conv import _ConvNd
from torch.nn.modules.batchnorm import _BatchNorm
import torch.nn.init as initer
import torch.nn.functional as F

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


def step_learning_rate(optimizer, base_lr, epoch, step_epoch, multiplier=0.1, clip=1e-6):
    """step learning rate policy"""
    lr = max(base_lr * (multiplier ** (epoch // step_epoch)), clip)
    for param_group in optimizer.param_groups:
        param_group['lr'] = lr


def poly_learning_rate(optimizer, base_lr, curr_iter, max_iter, power=0.9):
    """poly learning rate policy"""
    lr = base_lr * (1 - float(curr_iter) / max_iter) ** power
    for param_group in optimizer.param_groups:
        param_group['lr'] = lr


def intersectionAndUnion(output, target, K, ignore_index=255):
    # 'K' classes, output and target sizes are N or N * L or N * H * W, each value in range 0 to K - 1.
    assert (output.ndim in [1, 2, 3])
    assert output.shape == target.shape
    output = output.reshape(output.size).copy()
    target = target.reshape(target.size)
    output[np.where(target == ignore_index)[0]] = 255
    intersection = output[np.where(output == target)[0]]
    area_intersection, _ = np.histogram(intersection, bins=np.arange(K+1))
    area_output, _ = np.histogram(output, bins=np.arange(K+1))
    area_target, _ = np.histogram(target, bins=np.arange(K+1))
    area_union = area_output + area_target - area_intersection
    return area_intersection, area_union, area_target


def intersectionAndUnionGPU(output, target, K, ignore_index=255):
    # 'K' classes, output and target sizes are N or N * L or N * H * W, each value in range 0 to K - 1.
    assert (output.dim() in [1, 2, 3])
    assert output.shape == target.shape
    output = output.view(-1)
    target = target.view(-1)
    output[target == ignore_index] = ignore_index
    intersection = output[output == target]
    # https://github.com/pytorch/pytorch/issues/1382
    area_intersection = torch.histc(intersection.float().cpu(), bins=K, min=0, max=K-1)
    area_output = torch.histc(output.float().cpu(), bins=K, min=0, max=K-1)
    area_target = torch.histc(target.float().cpu(), bins=K, min=0, max=K-1)
    area_union = area_output + area_target - area_intersection
    return area_intersection.cuda(), area_union.cuda(), area_target.cuda()


def check_mkdir(dir_name):
    if not os.path.exists(dir_name):
        os.mkdir(dir_name)


def check_makedirs(dir_name):
    if not os.path.exists(dir_name):
        os.makedirs(dir_name)


def colorize(gray, palette):
    # gray: numpy array of the label and 1*3N size list palette
    color = Image.fromarray(gray.astype(np.uint8)).convert('P')
    color.putpalette(palette)
    return color


def group_weight(weight_group, module, norm_layer, lr):
    group_decay = []
    group_no_decay = []
    for m in module.modules():
        if isinstance(m, nn.Linear):
            group_decay.append(m.weight)
            if m.bias is not None:
                group_no_decay.append(m.bias)
        elif isinstance(m, (nn.Conv2d, nn.Conv3d)):
            group_decay.append(m.weight)
            if m.bias is not None:
                group_no_decay.append(m.bias)
        elif isinstance(m, norm_layer) or isinstance(m, nn.GroupNorm):
            if m.weight is not None:
                group_no_decay.append(m.weight)
            if m.bias is not None:
                group_no_decay.append(m.bias)

    assert len(list(module.parameters())) == len(group_decay) + len(group_no_decay)
    weight_group.append(dict(params=group_decay, lr=lr))
    weight_group.append(dict(params=group_no_decay, weight_decay=.0, lr=lr))
    return weight_group


def group_weight2(weight_group, module, norm_layer, lr):
    group_decay = []
    group_no_decay = []
    for m in module.modules():
        if isinstance(m, nn.Linear):
            group_decay.append(m.weight)
            if m.bias is not None:
                group_decay.append(m.bias)
        elif isinstance(m, (nn.Conv2d, nn.Conv3d)):
            group_decay.append(m.weight)
            if m.bias is not None:
                group_decay.append(m.bias)
        elif isinstance(m, norm_layer) or isinstance(m, nn.GroupNorm):
            if m.weight is not None:
                group_no_decay.append(m.weight)
            if m.bias is not None:
                group_no_decay.append(m.bias)

    assert len(list(module.parameters())) == len(group_decay) + len(group_no_decay)
    weight_group.append(dict(params=group_decay, lr=lr))
    weight_group.append(dict(params=group_no_decay, weight_decay=.0, lr=lr))
    return weight_group


def mixup_data(x, y, alpha=0.2):
    '''Returns mixed inputs, pairs of targets, and lambda'''
    if alpha > 0:
        lam = np.random.beta(alpha, alpha)
    else:
        lam = 1
    index = torch.randperm(x.shape[0])
    x = lam * x + (1 - lam) * x[index, :]
    y_a, y_b = y, y[index]
    return x, y_a, y_b, lam


def mixup_loss(output, target_a, target_b, lam=1.0, eps=0.0):
    w = torch.zeros_like(output).scatter(1, target_a.unsqueeze(1), 1)
    w = w * (1 - eps) + (1 - w) * eps / (output.shape[1] - 1)
    log_prob = F.log_softmax(output, dim=1)
    loss_a = (-w * log_prob).sum(dim=1).mean()

    w = torch.zeros_like(output).scatter(1, target_b.unsqueeze(1), 1)
    w = w * (1 - eps) + (1 - w) * eps / (output.shape[1] - 1)
    log_prob = F.log_softmax(output, dim=1)
    loss_b = (-w * log_prob).sum(dim=1).mean()
    return lam * loss_a + (1 - lam) * loss_b


def smooth_loss(output, target, eps=0.1):
    w = torch.zeros_like(output).scatter(1, target.unsqueeze(1), 1)
    w = w * (1 - eps) + (1 - w) * eps / (output.shape[1] - 1)
    log_prob = F.log_softmax(output, dim=1)
    loss = (-w * log_prob).sum(dim=1).mean()
    return loss


def cal_accuracy(output, target, topk=(1,)):
    """Computes the accuracy over the k top predictions for the specified values of k"""
    with torch.no_grad():
        maxk = max(topk)
        batch_size = target.size(0)
        _, pred = output.topk(maxk, 1, True, True)
        pred = pred.t()
        correct = pred.eq(target.view(1, -1).expand_as(pred))
        res = []
        for k in topk:
            correct_k = correct[:k].reshape(-1).float().sum(0, keepdim=True)
            res.append(correct_k.mul_(100.0 / batch_size))
        return res


def find_free_port():
    import socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    # Binding to port 0 will cause the OS to find an available port for us
    sock.bind(("", 0))
    port = sock.getsockname()[1]
    sock.close()
    # NOTE: there is still a chance the port could be taken by other processes.
    return port

def init_weights(model):
    for m in model.modules():
            if isinstance(m, nn.Conv2d):
                nn.init.kaiming_normal_(m.weight, mode='fan_out', nonlinearity='relu')
            elif isinstance(m, (nn.BatchNorm2d, nn.GroupNorm)):
                nn.init.constant_(m.weight, 1)
                nn.init.constant_(m.bias, 0)

def combination_cosine_similarity(mat1, mat2):
    """Calculates cosine similarity between every normalized vector of mat1 and every normalized vector of mat2.

    If vectors are of length f, then input dimensions must be (N, f) and (M, f), where N and M > 0, and result will be a tensor of dimensions (N, M).
    If result=combination_cosine_similarity(mat1, mat2), then result[i,j] is roughly equivalent to the value held by tensor
    torch.nn.functional.cosine_similarity(mat1[i].unsqueeze(0), mat2[j].unsqueeze(0)) for every position i, j.

    Args:
        mat1 (torch.tensor): first input
        mat2 (torch.tensor): second input

    Returns:
        torch.tensor: tensor with cosine distance results for every pairwise combination of vectors from mat1 to mat2.
    """
    mat1 = F.normalize(mat1)
    mat2 = F.normalize(mat2)
    return torch.mm(mat1, torch.transpose(mat2, 0, 1))

# from https://github.com/delijati/pytorch-siamese/blob/master/contrastive.py
class ContrastiveLoss(torch.nn.Module):
    """
    Contrastive loss function.
    
    This module does not normalize input vectors. If so desired, vectors must be normalized before hand.

    Shape:
        - x0: (N, f) where N is the batch size and f is the number of features for each vector (ie the length of each feature vector)
        - x1: (N, f), same shape as x0.
        - y: (N). This is the the target similarity, where y[i] should be 1 if embeddings x0[i] and x1[i] are expected to be similar and 0 if expected dissimilar. 
    """

    def __init__(self, margin = 1.0):
        super(ContrastiveLoss, self).__init__()
        self.margin = margin

    def check_type_forward(self, in_types):
        assert len(in_types) == 3

        x0_type, x1_type, y_type = in_types
        assert x0_type.size() == x1_type.shape
        assert x1_type.size()[0] == y_type.shape[0]
        assert x1_type.size()[0] > 0
        assert x0_type.dim() == 2
        assert x1_type.dim() == 2
        assert y_type.dim() == 1

    def forward(self, x0, x1, y):
        self.check_type_forward((x0, x1, y))

        # euclidian distance
        dist = F.pairwise_distance(x0, x1)

        mdist = self.margin - dist
        mdist = torch.clamp(mdist, min=0.0)
        loss = (y) * torch.pow(dist, 2) + (1 - y) * torch.pow(mdist, 2)

        # weight positive and negative contributions the same
        indices_dissimilar = (y==0).nonzero(as_tuple=True)
        indices_similar = (y==1).nonzero(as_tuple=True)
        loss_dissim = loss[indices_dissimilar]
        loss_sim = loss[indices_similar]
        loss_dissim = loss_dissim.mean()
        loss_sim = loss_sim.mean()
        loss = (loss_dissim + loss_sim) / 2.0
        return loss


class ContrastiveLossHardNegative(torch.nn.Module):
    def __init__(self, margin = 1.0):
        super(ContrastiveLossHardNegative, self).__init__()
        self.margin = margin

    def check_type_forward(self, in_types):
        assert len(in_types) == 4

        x0_type, x1_type, y0_type, y1_type = in_types
        assert x0_type.size() == x1_type.shape
        assert x1_type.size()[0] == y0_type.shape[0] == y1_type.shape[0]
        assert x1_type.size()[0] > 0
        assert x0_type.dim() == 2
        assert x1_type.dim() == 2
        assert y0_type.dim() == y1_type.dim() == 1

    def forward(self, x0, x1, y0, y1):
        self.check_type_forward((x0, x1, y0, y1))
        similar = y0==y1

        # separate positives and negatives, we're mining only negatives
        x0_sim = x0[similar]
        x1_sim = x1[similar]

        # calculate positive pairs loss
        dist = F.pairwise_distance(x0_sim, x1_sim)
        pos_loss = torch.pow(dist, 2).mean()

        # get all possible negative pairing distances
        distances = torch.cdist(x0, x1)
        dissimilar = y0.unsqueeze(1) != y1.unsqueeze(0)
        distances = distances[dissimilar]

        # get smallest negative pair distances (that maximize the loss)
        distances, _ = distances.flatten().sort()
        qty = min(x0.size(0)//2, distances.size(0)) # in unlikely case there's too few negative pairings
        distances = distances[:qty]

        # calculate hard negative loss
        mdist = self.margin - distances
        mdist = torch.clamp(mdist, min=0.0)
        neg_loss = torch.pow(mdist, 2).mean()
        loss = (pos_loss + neg_loss) / 2.0
        return loss
