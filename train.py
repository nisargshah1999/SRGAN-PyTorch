# Copyright 2021 Dakewe Biotech Corporation. All Rights Reserved.
# Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
import argparse
import logging
import os
import random
import time
import warnings

import torch
import torch.backends.cudnn as cudnn
import torch.distributed as dist
import torch.multiprocessing as mp
import torch.nn as nn
import torch.nn.parallel
import torch.optim
import torch.utils.data
import torch.utils.data.distributed
import torchvision.utils as vutils
from torch.utils.tensorboard import SummaryWriter

import srgan_pytorch.models as models
from srgan_pytorch.dataset import BaseTestDataset
from srgan_pytorch.dataset import BaseTrainDataset
from srgan_pytorch.loss import VGGLoss
from srgan_pytorch.models.discriminator import discriminator_for_vgg
from srgan_pytorch.utils.common import AverageMeter
from srgan_pytorch.utils.common import ProgressMeter
from srgan_pytorch.utils.common import configure
from srgan_pytorch.utils.common import create_folder
from srgan_pytorch.utils.estimate import test

model_names = sorted(name for name in models.__dict__
                     if name.islower() and not name.startswith("__")
                     and callable(models.__dict__[name]))

logger = logging.getLogger(__name__)
logging.basicConfig(format="[ %(levelname)s ] %(message)s", level=logging.DEBUG)

parser = argparse.ArgumentParser("Photo-Realistic Single Image Super-Resolution Using "
                                 "a Generative Adversarial Network.")
parser.add_argument("data", metavar="DIR",
                    help="Path to dataset")
parser.add_argument("-a", "--arch", metavar="ARCH", default="srgan",
                    choices=model_names,
                    help="Model architecture: " +
                         " | ".join(model_names) +
                         " (default: srgan)")
parser.add_argument("-j", "--workers", default=4, type=int, metavar="N",
                    help="Number of data loading workers. (default: 4)")
parser.add_argument("--psnr-epochs", default=20000, type=int, metavar="N",
                    help="Number of total psnr epochs to run. (default: 20000)")
parser.add_argument("--start-psnr-epoch", default=0, type=int, metavar='N',
                    help="Manual psnr epoch number (useful on restarts). (default: 0)")
parser.add_argument("--gan-epochs", default=4000, type=int, metavar="N",
                    help="Number of total gan epochs to run. (default: 4000)")
parser.add_argument("--start-gan-epoch", default=0, type=int, metavar='N',
                    help="Manual gan epoch number (useful on restarts). (default: 0)")
parser.add_argument("-b", "--batch-size", default=16, type=int,
                    metavar="N",
                    help="Mini-batch size (default: 16), this is the total "
                         "batch size of all GPUs on the current node when "
                         "using Data Parallel or Distributed Data Parallel")
parser.add_argument("--sampler-frequency", default=1, type=int, metavar="N",
                    help="If there are many datasets, this method can be used "
                         "to increase the number of epochs. (default:1)")
parser.add_argument("--psnr-lr", type=float, default=0.0001,
                    help="Learning rate for psnr-oral. (default: 0.0001)")
parser.add_argument("--gan-lr", type=float, default=0.0001,
                    help="Learning rate for gan-oral. (default: 0.0001)")
parser.add_argument("--image-size", type=int, default=96,
                    help="Image size of high resolution image. (default: 96)")
parser.add_argument("--upscale-factor", type=int, default=4, choices=[2, 4, 8],
                    help="Low to high resolution scaling factor. Optional: [2, 4, 8] (default: 4)")
parser.add_argument("--model-path", default="", type=str, metavar="PATH",
                    help="Path to latest checkpoint for model.")
parser.add_argument("--resume_psnr", default="", type=str, metavar="PATH",
                    help="Path to latest psnr-oral checkpoint.")
parser.add_argument("--resume_d", default="", type=str, metavar="PATH",
                    help="Path to latest -oral checkpoint.")
parser.add_argument("--resume_g", default="", type=str, metavar="PATH",
                    help="Path to latest psnr-oral checkpoint.")
parser.add_argument("--pretrained", dest="pretrained", action="store_true",
                    help="Use pre-trained model.")
parser.add_argument("--world-size", default=-1, type=int,
                    help="Number of nodes for distributed training")
parser.add_argument("--rank", default=-1, type=int,
                    help="Node rank for distributed training")
parser.add_argument("--dist-url", default="tcp://59.110.31.55:12345", type=str,
                    help="url used to set up distributed training. (default: tcp://59.110.31.55:12345)")
parser.add_argument("--dist-backend", default="nccl", type=str,
                    help="Distributed backend. (default: nccl)")
parser.add_argument("--seed", default=None, type=int,
                    help="Seed for initializing training.")
parser.add_argument("--gpu", default=None, type=int,
                    help="GPU id to use.")
parser.add_argument("--multiprocessing-distributed", action="store_true",
                    help="Use multi-processing distributed training to launch "
                         "N processes per node, which has N GPUs. This is the "
                         "fastest way to use PyTorch for either single node or "
                         "multi node data parallel training")


def main():
    args = parser.parse_args()

    if args.seed is not None:
        random.seed(args.seed)
        torch.manual_seed(args.seed)
        cudnn.deterministic = True
        warnings.warn("You have chosen to seed training. "
                      "This will turn on the CUDNN deterministic setting, "
                      "which can slow down your training considerably! "
                      "You may see unexpected behavior when restarting "
                      "from checkpoints.")

    if args.gpu is not None:
        logger.warning("You have chosen a specific GPU. This will completely disable data parallelism.")

    if args.dist_url == "env://" and args.world_size == -1:
        args.world_size = int(os.environ["WORLD_SIZE"])

    args.distributed = args.world_size > 1 or args.multiprocessing_distributed

    ngpus_per_node = torch.cuda.device_count()
    if args.multiprocessing_distributed:
        # Since we have ngpus_per_node processes per node, the total world_size
        # needs to be adjusted accordingly
        args.world_size = ngpus_per_node * args.world_size
        # Use torch.multiprocessing.spawn to launch distributed processes: the
        # main_worker process function
        mp.spawn(main_worker, nprocs=ngpus_per_node, args=(ngpus_per_node, args))
    else:
        # Simply call main_worker function
        main_worker(args.gpu, ngpus_per_node, args)


def main_worker(gpu, ngpus_per_node, args):
    args.gpu = gpu

    if args.gpu is not None:
        logger.info(f"Use GPU: {args.gpu} for training.")

    if args.distributed:
        if args.dist_url == "env://" and args.rank == -1:
            args.rank = int(os.environ["RANK"])
        if args.multiprocessing_distributed:
            # For multiprocessing distributed training, rank needs to be the
            # global rank among all the processes
            args.rank = args.rank * ngpus_per_node + gpu
        dist.init_process_group(backend=args.dist_backend,
                                init_method=args.dist_url,
                                world_size=args.world_size,
                                rank=args.rank)
    # create model
    generator = configure(args)
    discriminator = discriminator_for_vgg(args.image_size)

    if not torch.cuda.is_available():
        logger.warning("Using CPU, this will be slow.")
    elif args.distributed:
        # For multiprocessing distributed, DistributedDataParallel constructor
        # should always set the single device scope, otherwise,
        # DistributedDataParallel will use all available devices.
        if args.gpu is not None:
            torch.cuda.set_device(args.gpu)
            discriminator.cuda(args.gpu)
            generator.cuda(args.gpu)
            # When using a single GPU per process and per
            # DistributedDataParallel, we need to divide the batch size
            # ourselves based on the total number of GPUs we have
            args.batch_size = int(args.batch_size / ngpus_per_node)
            args.workers = int((args.workers + ngpus_per_node - 1) / ngpus_per_node)
            discriminator = nn.parallel.DistributedDataParallel(module=discriminator,
                                                                device_ids=[args.gpu],
                                                                find_unused_parameters=True)
            generator = nn.parallel.DistributedDataParallel(module=generator,
                                                            device_ids=[args.gpu],
                                                            find_unused_parameters=True)
        else:
            discriminator.cuda()
            generator.cuda()
            # DistributedDataParallel will divide and allocate batch_size to all
            # available GPUs if device_ids are not set
            discriminator = nn.parallel.DistributedDataParallel(discriminator, find_unused_parameters=True)
            generator = nn.parallel.DistributedDataParallel(generator, find_unused_parameters=True)
    elif args.gpu is not None:
        torch.cuda.set_device(args.gpu)
        discriminator = discriminator.cuda(args.gpu)
        generator = generator.cuda(args.gpu)
    else:
        # DataParallel will divide and allocate batch_size to all available GPUs
        if args.arch.startswith("alexnet") or args.arch.startswith("vgg"):
            discriminator.features = torch.nn.DataParallel(discriminator.features)
            generator.features = torch.nn.DataParallel(generator.features)
            discriminator.cuda()
            generator.cuda()
        else:
            discriminator = torch.nn.DataParallel(discriminator).cuda()
            generator = torch.nn.DataParallel(generator).cuda()

    # Loss = content loss + 0.001 * adversarial loss
    pixel_criterion = nn.MSELoss().cuda(args.gpu)
    # We use VGG5.4 as our feature extraction method by default.
    content_criterion = VGGLoss().cuda(args.gpu)
    adversarial_criterion = nn.BCELoss().cuda(args.gpu)
    logger.info(f"Losses function information:\n"
                f"\tPixel:       MSELoss\n"
                f"\tPerceptual:  VGG19_36th\n"
                f"\tAdversarial: BCELoss")

    # All optimizer function and scheduler function.
    psnr_optimizer = torch.optim.Adam(generator.parameters(), lr=args.psnr_lr, betas=(0.9, 0.999))
    discriminator_optimizer = torch.optim.Adam(discriminator.parameters(), lr=args.gan_lr, betas=(0.9, 0.999))
    generator_optimizer = torch.optim.Adam(generator.parameters(), lr=args.gan_lr, betas=(0.9, 0.999))
    discriminator_scheduler = torch.optim.lr_scheduler.StepLR(discriminator_optimizer, args.gan_epochs // 2, 0.1)
    generator_scheduler = torch.optim.lr_scheduler.StepLR(generator_optimizer, args.gan_epochs // 2, 0.1)
    logger.info(f"Optimizer information:\n"
                f"\tPSNR learning rate:          {args.psnr_lr}\n"
                f"\tDiscriminator learning rate: {args.gan_lr}\n"
                f"\tGenerator learning rate:     {args.gan_lr}\n"
                f"\tPSNR optimizer:              Adam, [betas=(0.9,0.999)]\n"
                f"\tDiscriminator optimizer:     Adam, [betas=(0.9,0.999)]\n"
                f"\tGenerator optimizer:         Adam, [betas=(0.9,0.999)]\n"
                f"\tPSNR scheduler:              None\n"
                f"\tDiscriminator scheduler:     StepLR, [step_size=self.gan_epochs // 2, gamma=0.1]\n"
                f"\tGenerator scheduler:         StepLR, [step_size=self.gan_epochs // 2, gamma=0.1]")

    logger.info("Load training dataset")
    # Selection of appropriate treatment equipment.
    train_dataset = BaseTrainDataset(root=os.path.join(args.data, "train"),
                                     image_size=args.image_size,
                                     upscale_factor=args.upscale_factor)
    test_dataset = BaseTestDataset(root=os.path.join(args.data, "test"),
                                   image_size=args.image_size,
                                   upscale_factor=args.upscale_factor)

    if args.distributed:
        train_sampler = torch.utils.data.distributed.DistributedSampler(train_dataset)
    else:
        train_sampler = None

    train_dataloader = torch.utils.data.DataLoader(train_dataset,
                                                   batch_size=args.batch_size,
                                                   shuffle=(train_sampler is None),
                                                   pin_memory=True,
                                                   sampler=train_sampler,
                                                   num_workers=args.workers)
    test_dataloader = torch.utils.data.DataLoader(test_dataset,
                                                  batch_size=args.batch_size,
                                                  shuffle=False,
                                                  pin_memory=True,
                                                  num_workers=args.workers)

    logger.info(f"Dataset information:\n"
                f"\tTrain Path:              {os.getcwd()}/{args.data}/train\n"
                f"\tTest Path:               {os.getcwd()}/{args.data}/test\n"
                f"\tNumber of train samples: {len(train_dataset)}\n"
                f"\tNumber of test samples:  {len(test_dataset)}\n"
                f"\tNumber of train batches: {len(train_dataloader)}\n"
                f"\tNumber of test batches:  {len(test_dataloader)}\n"
                f"\tShuffle of train:        True\n"
                f"\tShuffle of test:         False\n"
                f"\tSampler of train:        {bool(train_sampler)}\n"
                f"\tSampler of test:         None\n"
                f"\tWorkers of train:        {args.workers}\n"
                f"\tWorkers of test:         {args.workers}")

    # optionally resume from a checkpoint
    if args.resume_psnr:
        if os.path.isfile(args.resume_psnr):
            logger.info(f"Loading checkpoint '{args.resume_psnr}'.")
            if args.gpu is None:
                checkpoint = torch.load(args.resume_psnr)
            else:
                # Map model to be loaded to specified single gpu.
                checkpoint = torch.load(args.resume_psnr, map_location=f"cuda:{args.gpu}")
            args.start_psnr_epoch = checkpoint["epoch"]
            generator.load_state_dict(checkpoint["state_dict"])
            psnr_optimizer.load_state_dict(checkpoint["optimizer"])
            logger.info(f"Loaded checkpoint '{args.resume_psnr}' (epoch {checkpoint['epoch']}).")
        else:
            logger.info(f"No checkpoint found at '{args.resume_psnr}'.")

    if args.resume_d or args.resume_g:
        if os.path.isfile(args.resume_d) or os.path.isfile(args.resume_g):
            logger.info(f"Loading checkpoint '{args.resume_d}'.")
            logger.info(f"Loading checkpoint '{args.resume_g}'.")
            if args.gpu is None:
                checkpoint_d = torch.load(args.resume_d)
                checkpoint_g = torch.load(args.resume_g)
            else:
                # Map model to be loaded to specified single gpu.
                checkpoint_d = torch.load(args.resume_d, map_location=f"cuda:{args.gpu}")
                checkpoint_g = torch.load(args.resume_g, map_location=f"cuda:{args.gpu}")
            args.start_gan_epoch = checkpoint_d["epoch"]
            discriminator.load_state_dict(checkpoint_d["state_dict"])
            discriminator_optimizer.load_state_dict(checkpoint_d["optimizer"])
            generator.load_state_dict(checkpoint_g["state_dict"])
            generator_optimizer.load_state_dict(checkpoint_g["optimizer"])
            logger.info(f"Loaded checkpoint '{args.resume_d}' (epoch {checkpoint_d['epoch']}).")
            logger.info(f"Loaded checkpoint '{args.resume_g}' (epoch {checkpoint_g['epoch']}).")
        else:
            logger.info(f"No checkpoint found at '{args.resume_d}' or '{args.resume_g}'.")

    cudnn.benchmark = True

    # Create a SummaryWriter at the beginning of training.
    psnr_writer = SummaryWriter(f"runs/{args.arch}_psnr_logs")
    gan_writer = SummaryWriter(f"runs/{args.arch}_gan_logs")

    logger.info(f"Train information:\n"
                f"\tPSNR-oral epochs: {args.psnr_epochs}\n"
                f"\tGAN-oral epochs:  {args.gan_epochs}")

    for epoch in range(args.start_psnr_epoch, args.psnr_epochs):
        if args.distributed:
            train_sampler.set_epoch(epoch)

        # train for one epoch
        train_psnr(train_dataloader=train_dataloader,
                   generator=generator,
                   pixel_criterion=pixel_criterion,
                   psnr_optimizer=psnr_optimizer,
                   epoch=epoch,
                   writer=psnr_writer,
                   args=args)

        # Test for every epoch.
        psnr_value, ssim_value, lpips_value, gmsd_value = test(generator, test_dataloader, args.gpu)
        gan_writer.add_scalar("Test/PSNR", psnr_value, epoch + 1)
        gan_writer.add_scalar("Test/SSIM", ssim_value, epoch + 1)
        gan_writer.add_scalar("Test/LPIPS", lpips_value, epoch + 1)
        gan_writer.add_scalar("Test/GMSD", gmsd_value, epoch + 1)

        if not args.multiprocessing_distributed or (
                args.multiprocessing_distributed and args.rank % ngpus_per_node == 0):
            torch.save({"epoch": epoch + 1,
                        "arch": args.arch,
                        "state_dict": generator.module.state_dict() if args.multiprocessing_distributed else generator.state_dict(),
                        "optimizer": psnr_optimizer.state_dict(),
                        }, os.path.join("weights", f"PSNR_epoch{epoch}.pth"))

        # Load best PSNR model.
        generator.load_state_dict(torch.load(os.path.join("weights", f"PSNR.pth"), map_location=f"cuda:{args.gpu}"))

    for epoch in range(args.start_gan_epoch, args.gan_epochs):
        if args.distributed:
            train_sampler.set_epoch(epoch)

        # train for one epoch
        train_gan(train_dataloader=train_dataloader,
                  discriminator=discriminator,
                  generator=generator,
                  content_criterion=content_criterion,
                  adversarial_criterion=adversarial_criterion,
                  discriminator_optimizer=discriminator_optimizer,
                  generator_optimizer=generator_optimizer,
                  epoch=epoch,
                  writer=gan_writer,
                  args=args)

        discriminator_scheduler.step()
        generator_scheduler.step()

        # Test for every epoch.
        psnr_value, ssim_value, lpips_value, gmsd_value = test(generator, test_dataloader, args.gpu)
        gan_writer.add_scalar("Test/PSNR", psnr_value, epoch + 1)
        gan_writer.add_scalar("Test/SSIM", ssim_value, epoch + 1)
        gan_writer.add_scalar("Test/LPIPS", lpips_value, epoch + 1)
        gan_writer.add_scalar("Test/GMSD", gmsd_value, epoch + 1)

        if not args.multiprocessing_distributed or (
                args.multiprocessing_distributed and args.rank % ngpus_per_node == 0):
            torch.save({"epoch": epoch + 1,
                        "arch": "vgg",
                        "state_dict": discriminator.module.state_dict() if args.multiprocessing_distributed else discriminator.state_dict(),
                        "optimizer": discriminator_optimizer.state_dict()
                        }, os.path.join("weights", f"Discriminator_epoch{epoch}.pth"))
            torch.save({"epoch": epoch + 1,
                        "arch": args.arch,
                        "state_dict": generator.module.state_dict() if args.multiprocessing_distributed else generator.state_dict(),
                        "optimizer": generator_optimizer.state_dict()
                        }, os.path.join("weights", f"Generator_epoch{epoch}.pth"))


def train_psnr(train_dataloader: torch.utils.data.DataLoader,
               generator: nn.Module,
               pixel_criterion: nn.MSELoss,
               psnr_optimizer: torch.optim.Adam,
               epoch: int,
               writer: SummaryWriter,
               args: argparse.ArgumentParser.parse_args):
    batch_time = AverageMeter("Time", ":6.4f")
    mse_losses = AverageMeter("MSE Loss", ":.6f")
    progress = ProgressMeter(len(train_dataloader), [batch_time, mse_losses], prefix=f"Epoch: [{epoch}]")

    # switch to train mode
    generator.train()

    end = time.time()
    for i, (lr, hr) in enumerate(train_dataloader):
        # Move data to special device.
        if args.gpu is not None:
            lr = lr.cuda(args.gpu, non_blocking=True)
            hr = hr.cuda(args.gpu, non_blocking=True)

        generator.zero_grad()

        # Generating fake high resolution images from real low resolution images.
        sr = generator(lr)
        # The MSE Loss of the generated fake high-resolution image and real high-resolution image is calculated.
        mse_loss = pixel_criterion(sr, hr)
        mse_loss.backward()
        psnr_optimizer.step()

        # measure elapsed time
        batch_time.update(time.time() - end)
        end = time.time()

        # measure accuracy and record loss
        mse_losses.update(mse_loss.item(), lr.size(0))

        iters = i + epoch * len(train_dataloader) + 1
        writer.add_scalar("Train/MSE Loss", mse_loss.item(), iters)

        # Output results every 100 batches.
        if i % 100 == 0:
            progress.display(i)

        # Save image every 1000 batches.
        if iters % 1000 == 0:
            vutils.save_image(hr, os.path.join("runs", "hr", f"PSNR_{iters}.bmp"))
            sr = generator(lr)
            vutils.save_image(sr.detach(), os.path.join("runs", "sr", f"PSNR_{iters}.bmp"))


def train_gan(train_dataloader: torch.utils.data.DataLoader,
              discriminator: nn.Module,
              generator: nn.Module,
              content_criterion: VGGLoss,
              adversarial_criterion: nn.BCELoss,
              discriminator_optimizer: torch.optim.Adam,
              generator_optimizer: torch.optim.Adam,
              epoch: int,
              writer: SummaryWriter,
              args: argparse.ArgumentParser.parse_args):
    batch_time = AverageMeter("Time", ":.4f")
    d_losses = AverageMeter("D Loss", ":.6f")
    g_losses = AverageMeter("G Loss", ":.6f")
    content_losses = AverageMeter("Content Loss", ":.4f")
    adversarial_losses = AverageMeter("Adversarial Loss", ":.4f")
    d_hr_values = AverageMeter("D(x)", ":.4f")
    d_sr1_values = AverageMeter("D(SR1)", ":.4f")
    d_sr2_values = AverageMeter("D(SR2)", ":.4f")

    progress = ProgressMeter(
        len(train_dataloader),
        [batch_time,
         d_losses, g_losses,
         content_losses, adversarial_losses,
         d_hr_values, d_sr1_values, d_sr2_values],
        prefix=f"Epoch: [{epoch}]")

    # switch to train mode
    discriminator.train()
    generator.train()

    end = time.time()
    for i, (lr, hr) in enumerate(train_dataloader):
        # Move data to special device.
        if args.gpu is not None:
            lr = lr.cuda(args.gpu, non_blocking=True)
            hr = hr.cuda(args.gpu, non_blocking=True)
        batch_size = lr.size(0)

        # The real sample label is 1, and the generated sample label is 0.
        real_label = torch.full((batch_size, 1), 1, dtype=lr.dtype).cuda(args.gpu, non_blocking=True)
        fake_label = torch.full((batch_size, 1), 0, dtype=lr.dtype).cuda(args.gpu, non_blocking=True)

        ##############################################
        # (1) Update D network: maximize - E(hr)[log(D(hr))] + E(lr)[log(1- D(G(lr))]
        ##############################################
        # Set discriminator gradients to zero.
        discriminator.zero_grad()

        real_output = discriminator(hr)
        # Let the discriminator realize that the sample is real.
        d_loss_real = adversarial_criterion(real_output, real_label)

        # Generating fake high resolution images from real low resolution images.
        sr = generator(lr)
        fake_output = discriminator(sr.detach())
        # Let the discriminator realize that the sample is false.
        d_loss_fake = adversarial_criterion(fake_output, fake_label)

        # Count all discriminator losses.
        d_loss = (d_loss_real + d_loss_fake) / 2
        d_loss.backward()
        d_hr = real_output.mean().item()
        d_sr1 = fake_output.mean().item()

        # Update discriminator optimizer gradient information.
        discriminator_optimizer.step()

        ##############################################
        # (2) Update G network: content loss + 0.001 * adversarial loss
        ##############################################
        # Set discriminator gradients to zero.
        generator.zero_grad()

        # Based on VGG19_36th pre training model to find the maximum square error between feature maps.
        content_loss = content_criterion(sr, hr.detach())

        fake_output = discriminator(sr)
        # Let the discriminator realize that the sample is true.
        adversarial_loss = adversarial_criterion(fake_output, real_label)
        g_loss = content_loss + 0.001 * adversarial_loss
        g_loss.backward()
        d_sr2 = fake_output.mean().item()

        # Update generator optimizer gradient information.
        generator_optimizer.step()

        # measure accuracy and record loss
        d_losses.update(d_loss.item(), lr.size(0))
        g_losses.update(g_loss.item(), lr.size(0))
        content_losses.update(content_loss.item(), lr.size(0))
        adversarial_losses.update(adversarial_loss.item(), lr.size(0))
        d_hr_values.update(d_hr, lr.size(0))
        d_sr1_values.update(d_sr1, lr.size(0))
        d_sr2_values.update(d_sr2, lr.size(0))

        # measure elapsed time
        batch_time.update(time.time() - end)
        end = time.time()

        iters = i + epoch * len(train_dataloader) + 1
        writer.add_scalar("Train/D Loss", d_loss.item(), iters)
        writer.add_scalar("Train/G Loss", d_loss.item(), iters)
        writer.add_scalar("Train/Content Loss", content_loss.item(), iters)
        writer.add_scalar("Train/Adversarial Loss", adversarial_loss.item(), iters)
        writer.add_scalar("Train/D(LR)", d_hr, iters)
        writer.add_scalar("Train/D(SR1)", d_sr1, iters)
        writer.add_scalar("Train/D(SR2)", d_sr2, iters)

        # Output results every 100 batches.
        if i % 100 == 0:
            progress.display(i)

        # Save image every 1000 batches.
        if iters % 1000 == 0:
            vutils.save_image(hr, os.path.join("runs", "hr", f"GAN_{iters}.bmp"))
            sr = generator(lr)
            vutils.save_image(sr.detach(), os.path.join("runs", "sr", f"GAN_{iters}.bmp"))

    if __name__ == "__main__":
        print("##################################################\n")
        print("Run Training Engine.\n")

        create_folder("runs")
        create_folder("runs/hr")
        create_folder("runs/sr")
        create_folder("weights")

        logger.info("TrainingEngine:")
        print("\tAPI version .......... 0.1.0")
        print("\tBuild ................ 2021.03.25")
        print("##################################################\n")
        main()
        logger.info("All training has been completed successfully.\n")
