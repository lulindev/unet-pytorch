import os

import torch.distributed
import torch.utils.data
import torch.utils.tensorboard
import tqdm

import datasets
import eval
import utils

if __name__ == '__main__':
    # 0. Load cfg and create components builder
    cfg = utils.builder.load_cfg()
    builder = utils.builder.Builder(cfg)

    # Distributed Data-Parallel Training (DDP)
    if cfg['ddp']:
        assert torch.distributed.is_nccl_available(), 'NCCL backend is not available.'
        torch.distributed.init_process_group(backend='nccl', init_method='env://')
        assert torch.distributed.is_initialized(), 'Distributed Data-Parallel is not initialized.'
        local_rank = torch.distributed.get_rank()
        world_size = torch.distributed.get_world_size()
        print("local_rank:", local_rank)
        print("world_size:", world_size)
    else:
        local_rank = 0

    # 1. Dataset
    trainset, trainloader = builder.build_dataset('train')
    _, valloader = builder.build_dataset('val')

    # 2. Model
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu', local_rank)
    model = builder.build_model(trainset.num_classes).to(device)
    if cfg['ddp']:
        model = torch.nn.parallel.DistributedDataParallel(model)
    model_name = cfg['model']['name']
    amp_enabled = cfg['model']['amp_enabled']
    print(f'Activated model: {model_name}')

    # 3. Loss function, optimizer, lr scheduler, scaler
    criterion = builder.build_criterion(trainset.ignore_index)
    optimizer = builder.build_optimizer(model)
    scheduler = builder.build_scheduler(optimizer)
    scaler = torch.cuda.amp.GradScaler(enabled=amp_enabled)

    # Resume training at checkpoint
    if cfg['resume_training'] is not None:
        path = cfg['resume_training']
        if cfg['ddp']:
            torch.distributed.barrier()
            checkpoint = torch.load(path, map_location={'cuda:0': f'cuda:{local_rank}'})
        else:
            checkpoint = torch.load(path)
        model.load_state_dict(checkpoint['model_state_dict'])
        if cfg['fine_tuning_batchnorm']:
            model.freeze_bn()
        else:
            optimizer.load_state_dict(checkpoint['optimizer_state_dict'])
            scheduler.load_state_dict(checkpoint['scheduler_state_dict'])
            scaler.load_state_dict(checkpoint['scaler_state_dict'])
        start_epoch = checkpoint['epoch'] + 1
        prev_miou = checkpoint['miou']
        prev_val_loss = checkpoint['val_loss']
        print(f'Resume training. {path}')
    else:
        start_epoch = 0
        prev_miou = 0.0
        prev_val_loss = 100

    # 4. Tensorboard
    writer = torch.utils.tensorboard.SummaryWriter(os.path.join('runs', model_name))

    # 5. Train and evaluate
    log_loss = tqdm.tqdm(total=0, position=2, bar_format='{desc}', leave=False)
    for epoch in tqdm.tqdm(range(start_epoch, cfg[model_name]['epoch']), desc='Epoch'):
        if utils.train_interupter.train_interupter():
            print('Train interrupt occurs.')
            break
        model.train()
        trainloader.sampler.set_epoch(epoch)

        for batch_idx, (images, targets) in enumerate(tqdm.tqdm(trainloader, desc='Train', leave=False)):
            iters = len(trainloader) * epoch + batch_idx
            images, targets = images.to(device), targets.to(device)

            optimizer.zero_grad(set_to_none=True)
            with torch.cuda.amp.autocast(enabled=amp_enabled):
                outputs = model(images)
                loss = criterion(outputs, targets)
            scaler.scale(loss).backward()
            scaler.step(optimizer)
            scaler.update()

            writer.add_scalar('loss/training', loss.item(), iters)
            log_loss.set_description_str(f'Loss: {loss.item():.4f}')

            writer.add_scalar('lr', optimizer.param_groups[0]['lr'], iters)
            scheduler.step()

        val_loss, _, miou, _ = eval.evaluate(model, valloader, criterion, trainset.num_classes, amp_enabled, device)
        writer.add_scalar('loss/validation', val_loss, epoch)
        writer.add_scalar('metrics/mIoU', miou, epoch)

        images, targets = valloader.__iter__().__next__()
        images, targets = images[2:4].to(device), targets[2:4]
        with torch.no_grad():
            outputs = model(images)
            outputs = torch.argmax(outputs, dim=1)
        targets = datasets.utils.decode_segmap_to_color_image(
            targets, trainset.colors, trainset.num_classes, trainset.ignore_index, trainset.ignore_color
        )
        outputs = datasets.utils.decode_segmap_to_color_image(
            outputs, trainset.colors, trainset.num_classes, trainset.ignore_index, trainset.ignore_color
        )
        if epoch == 0:
            writer.add_images('eval/0Groundtruth', targets, epoch)
        writer.add_images('eval/1' + model_name, outputs, epoch)

        # Save checkpoint
        if local_rank == 0:
            os.makedirs('weights', exist_ok=True)
            torch.save({
                'model_state_dict': model.state_dict(),
                'optimizer_state_dict': optimizer.state_dict(),
                'scheduler_state_dict': scheduler.state_dict(),
                'scaler_state_dict': scaler.state_dict(),
                'epoch': epoch,
                'miou': miou,
                'val_loss': val_loss
            }, os.path.join('weights', f'{model_name}_checkpoint.pth'))

            # Save best mIoU model
            if miou > prev_miou:
                torch.save(model.state_dict(), os.path.join('weights', f'{model_name}_best_miou.pth'))
                prev_miou = miou

            # Save best val_loss model
            if val_loss < prev_val_loss:
                torch.save(model.state_dict(), os.path.join('weights', f'{model_name}_best_val_loss.pth'))
                prev_val_loss = val_loss
    writer.close()
    torch.distributed.destroy_process_group()
