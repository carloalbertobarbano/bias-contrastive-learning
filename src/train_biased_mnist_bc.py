import argparse
import datetime
import logging
import os
import time
from pathlib import Path

import numpy as np
import torch
from torch import nn, optim

from debias.datasets.biased_mnist import get_color_mnist
from debias.losses.bias_contrastive import BiasContrastiveLoss
from debias.networks.simple_conv import SimpleConvNet
from debias.utils.logging import set_logging
from debias.utils.utils import (AverageMeter, MultiDimAverageMeter, accuracy,
                                save_model, set_seed, pretty_dict)


def parse_option():
    parser = argparse.ArgumentParser()
    parser.add_argument('--exp_name', type=str, default='test', )
    parser.add_argument('--gpu', type=int, default=0)

    parser.add_argument('--print_freq', type=int, default=300,
                        help='print frequency')
    parser.add_argument('--save_freq', type=int, default=200,
                        help='save frequency')
    parser.add_argument('--epochs', type=int, default=80,
                        help='number of training epochs')
    parser.add_argument('--seed', type=int, default=1)
    parser.add_argument('--corr', type=float, default=0.999)

    parser.add_argument('--bs', type=int, default=128, help='batch_size')
    parser.add_argument('--cbs', type=int, default=64, help='batch_size of dataloader for contrastive loss')
    parser.add_argument('--lr', type=float, default=1e-3)

    # hyperparameters
    parser.add_argument('--weight', type=float, default=0.01)
    parser.add_argument('--ratio', type=int, default=10)
    parser.add_argument('--aug', type=int, default=1)
    parser.add_argument('--jitter', type=int, default=1)
    parser.add_argument('--bb', type=int, default=0)

    opt = parser.parse_args()
    os.environ['CUDA_VISIBLE_DEVICES'] = str(opt.gpu)

    return opt


def set_model(train_loader, opt):
    model = SimpleConvNet().cuda()
    criterion = BiasContrastiveLoss(
        confusion_matrix=train_loader.dataset.confusion_matrix,
        bb=opt.bb)

    return model, criterion


def train(train_loader, cont_train_loader, model, criterion, optimizer, epoch, opt):
    model.train()
    avg_ce_loss = AverageMeter()
    avg_con_loss = AverageMeter()
    avg_loss = AverageMeter()

    train_iter = iter(train_loader)
    cont_train_iter = iter(cont_train_loader)
    for idx, (images, labels, biases, _) in enumerate(train_iter):
        try:
            cont_images, cont_labels, cont_biases, _ = next(cont_train_iter)
        except:
            cont_train_iter = iter(cont_train_loader)
            cont_images, cont_labels, cont_biases, _ = next(cont_train_iter)

        bsz = labels.shape[0]
        cont_bsz = cont_labels.shape[0]

        labels, biases = labels.cuda(), biases.cuda()

        images = images.cuda()
        logits, _ = model(images)

        total_images = torch.cat([cont_images[0], cont_images[1]], dim=0)
        total_images, cont_labels, cont_biases = total_images.cuda(), cont_labels.cuda(), cont_biases.cuda()
        _, cont_features = model(total_images)

        f1, f2 = torch.split(cont_features, [cont_bsz, cont_bsz], dim=0)
        cont_features = torch.cat([f1.unsqueeze(1), f2.unsqueeze(1)], dim=1)

        ce_loss, con_loss = criterion(logits, labels, biases, cont_features, cont_labels, cont_biases)

        loss = ce_loss * opt.weight + con_loss

        avg_ce_loss.update(ce_loss.item(), bsz)
        avg_con_loss.update(con_loss.item(), bsz)
        avg_loss.update(loss.item(), bsz)

        optimizer.zero_grad()
        loss.backward()
        optimizer.step()

    return avg_ce_loss.avg, avg_con_loss.avg, avg_loss.avg


def validate(val_loader, model):
    model.eval()

    top1 = AverageMeter()
    attrwise_acc_meter = MultiDimAverageMeter(dims=(10, 10))

    with torch.no_grad():
        for idx, (images, labels, biases, _) in enumerate(val_loader):
            images, labels, biases = images.cuda(), labels.cuda(), biases.cuda()
            bsz = labels.shape[0]

            output, _ = model(images)
            preds = output.data.max(1, keepdim=True)[1].squeeze(1)

            acc1, = accuracy(output, labels, topk=(1,))
            top1.update(acc1[0], bsz)

            corrects = (preds == labels).long()
            attrwise_acc_meter.add(corrects.cpu(), torch.stack([labels.cpu(), biases.cpu()], dim=1))

    return top1.avg, attrwise_acc_meter.get_unbiased_acc()


def main():
    opt = parse_option()

    exp_name = f'bc-bb{opt.bb}-color_mnist_corr{opt.corr}-{opt.exp_name}-lr{opt.lr}-bs{opt.bs}-cbs{opt.cbs}-w{opt.weight}-ratio{opt.ratio}-aug{opt.aug}-seed{opt.seed}'
    opt.exp_name = exp_name

    output_dir = f'/scratch/bias-contrastive-learning/exp_results/{exp_name}'
    save_path = Path(output_dir)
    save_path.mkdir(parents=True, exist_ok=True)

    set_logging(exp_name, 'INFO', str(save_path))
    set_seed(opt.seed)
    logging.info(f'save_path: {save_path}')

    np.set_printoptions(precision=3)
    torch.set_printoptions(precision=3)

    root = './data/biased_mnist'
    train_loader = get_color_mnist(
        root,
        batch_size=opt.bs,
        data_label_correlation=opt.corr,
        n_confusing_labels=9,
        split='train',
        seed=opt.seed,
        aug=False, )

    logging.info(
        f'confusion_matrix - \n original: {train_loader.dataset.confusion_matrix_org}, \n normalized: {train_loader.dataset.confusion_matrix}')

    cont_train_loader = get_color_mnist(
        root,
        batch_size=opt.cbs,
        data_label_correlation=opt.corr,
        n_confusing_labels=9,
        split='train',
        seed=opt.seed,
        aug=opt.aug,
        jitter=opt.jitter,
        two_crop=True,
        ratio=opt.ratio,
        given_y=True)

    val_loaders = {}
    val_loaders['valid'] = get_color_mnist(
        root,
        batch_size=256,
        data_label_correlation=0.1,
        n_confusing_labels=9,
        split='train_val',
        seed=opt.seed,
        aug=False,
    )
    val_loaders['test'] = get_color_mnist(
        root,
        batch_size=256,
        data_label_correlation=0.1,
        n_confusing_labels=9,
        split='valid',
        seed=opt.seed,
        aug=False)

    model, criterion = set_model(train_loader, opt)

    decay_epochs = [opt.epochs // 3, opt.epochs * 2 // 3]

    optimizer = optim.Adam(model.parameters(), lr=opt.lr, weight_decay=1e-4)
    scheduler = torch.optim.lr_scheduler.MultiStepLR(optimizer, milestones=decay_epochs, gamma=0.1)
    logging.info(f"decay_epochs: {decay_epochs}")

    (save_path / 'checkpoints').mkdir(parents=True, exist_ok=True)

    best_accs = {'valid': 0, 'test': 0}
    best_epochs = {'valid': 0, 'test': 0}
    best_stats = {}
    start_time = time.time()
    for epoch in range(1, opt.epochs + 1):
        logging.info(f'[{epoch} / {opt.epochs}] Learning rate: {scheduler.get_last_lr()[0]} weight: {opt.weight}')
        ce_loss, con_loss, loss = train(train_loader, cont_train_loader, model, criterion, optimizer, epoch, opt)
        logging.info(f'[{epoch} / {opt.epochs}] Loss: {loss} CE Loss: {ce_loss} Con Loss: {con_loss}')

        scheduler.step()

        stats = pretty_dict(epoch=epoch)
        for key, val_loader in val_loaders.items():
            _, acc_unbiased = validate(val_loader, model)
            stats[f'{key}/acc_unbiased'] = acc_unbiased.item() * 100

        logging.info(f'[{epoch} / {opt.epochs}] {stats}')
        for tag in best_accs.keys():
            if stats[f'{tag}/acc_unbiased'] > best_accs[tag]:
                best_accs[tag] = stats[f'{tag}/acc_unbiased']
                best_epochs[tag] = epoch
                best_stats[tag] = pretty_dict(**{f'best_{tag}_{k}': v for k, v in stats.items()})

                save_file = save_path / 'checkpoints' / f'best_{tag}.pth'
                save_model(model, optimizer, opt, epoch, save_file)
            logging.info(
                f'[{epoch} / {opt.epochs}] best {tag} accuracy: {best_accs[tag]:.3f} at epoch {best_epochs[tag]} \n best_stats: {best_stats[tag]}')

    total_time = time.time() - start_time
    total_time_str = str(datetime.timedelta(seconds=int(total_time)))
    logging.info(f'Total training time: {total_time_str}')

    save_file = save_path / 'checkpoints' / f'last.pth'
    save_model(model, optimizer, opt, opt.epochs, save_file)


if __name__ == '__main__':
    main()
