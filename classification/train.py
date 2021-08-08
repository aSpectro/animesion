import time
from datetime import timedelta
import os
import logging
import argparse
import pandas as pd
from statistics import mean

import torch
from torchsummary import summary
import wandb
from transformers import BertTokenizer

import utilities as utilities

logger = logging.getLogger(__name__)

def environment_loader(args):

    # Init logger    
    wandb.init(config=args)
    wandb.run.name = '{}'.format(args.run_name)
    file_name = '{}_log.txt'.format(args.run_name)
    f = open(os.path.join(args.results_dir, '{}'.format(file_name)), 'w', buffering=1)
    utilities.misc.print_write(f, str(args))
    
    # Set device and random seed
    device = torch.device('cuda:0' if torch.cuda.is_available() else 'cpu')
    utilities.misc.set_seed(args.seed)

    # dataloader and train/test datasets
    train_set, train_loader = utilities.data_selection.load_data(args, split='train')
    _, val_loader = utilities.data_selection.load_data(args, split='val')
    _, test_loader = utilities.data_selection.load_data(args, split='test')
    args.num_classes = train_set.num_classes
    classid_classname_dic = train_set.classes

    # model
    model = utilities.model_selection.load_model(args, device)
    utilities.misc.print_write(f, str(model.configuration))
    if (not args.interm_features_fc) and (not args.multimodal):
        summary(model, input_size=iter(train_loader).next()[0].shape[1:])
    
    # loss and optimizer
    params_to_update = []
    for param in model.parameters():
        if param.requires_grad == True:
            params_to_update.append(param)
    optimizer = torch.optim.SGD(params_to_update, lr=args.learning_rate, momentum=0.9)

    steps_per_epoch = len(train_loader)
    total_steps = args.no_epochs * steps_per_epoch
    if args.lr_scheduler == 'warmupCosine':
        lr_scheduler = utilities.scheduler.WarmupCosineSchedule(optimizer, 
        warmup_steps=args.warmup_steps, t_total=total_steps)
    else:
        lr_scheduler=None
    
    # mask scheduler
    mask_wucd_steps = int(total_steps * args.mask_wucd_percent)
    mask_scheduler = utilities.scheduler.MasksSchedule(mask_schedule=args.mask_schedule, batch_size=args.batch_size, 
        total_seq_len=model.configuration.seq_len, max_text_seq_len=args.max_text_seq_len,
        warmup_steps=mask_wucd_steps, cooldown_steps=mask_wucd_steps, total_steps=total_steps,
        cycles=.5)
    if args.mask_schedule:
        tokenizer = BertTokenizer.from_pretrained('bert-base-uncased')
    else:
        tokenizer = None
        
    return [f, device, train_set, train_loader, val_loader, test_loader, 
    classid_classname_dic, model, optimizer, lr_scheduler, mask_scheduler, tokenizer]
    

def train_one_epoch(args, f, epoch, global_step, model, device, tokenizer,
    optimizer, mask_scheduler, lr_scheduler, train_loader, train_loss_avg):

    criterion = torch.nn.CrossEntropyLoss()
    if args.mask_schedule:
        criterion_mlm = torch.nn.CrossEntropyLoss(reduction='none')

    model.train()
    current_losses = []
    steps_per_epoch = len(train_loader)
    
    for i, batch in enumerate(train_loader):

        if args.multimodal:
            images, labels, captions = batch
            captions = captions.squeeze(dim=1).to(device)
        else:
            images, labels = batch
        images = images.to(device)
        labels = labels.to(device)

        # return new masks according to schedule
        masks = mask_scheduler.ret_mask(global_step)
        if masks is not None:
            # 0 is [PAD], 101 is [CLS], 102 is [SEP]
            labels_text = torch.where((captions==0) | (captions==101) | (captions==102), -100, captions)
            labels_text = labels_text.to(device)
            masks = masks.to(device)                
            labels_text = torch.where(masks[:, -args.max_text_seq_len:]==1, -100, labels_text)
        
        # Forward pass
        if args.multimodal and args.mask_schedule and args.exclusion_loss:
            outputs, outputs_text, exclusion_loss = model(images, text=captions, mask=masks)
        elif args.multimodal and args.mask_schedule:
            outputs, outputs_text = model(images, text=captions, mask=masks)
        elif args.multimodal and args.exclusion_loss:
            outputs, exclusion_loss = model(images, text=captions, mask=masks)
        elif args.multimodal:
            outputs = model(images, text=captions, mask=masks)
        elif args.exclusion_loss:
            outputs, exclusion_loss = model(images)
        else:
            outputs = model(images)
        
        loss = criterion(outputs, labels)
        if masks is not None:
            loss = loss + criterion(outputs_text.transpose(1, 2), labels_text)
        if args.exclusion_loss:
            loss =  loss - (args.exclusion_weight * (args.temperature ** 2) * exclusion_loss)
            
        # Backward and optimize
        loss.backward()
        optimizer.step()
        if lr_scheduler:
            lr_scheduler.step()
        optimizer.zero_grad()
        
        current_losses.append(loss.item()) 

        # update global and return new masks according to schedule
        global_step[0] += 1
        
        # prints current set of results after each args.log_freq iterations
        if (i % args.log_freq) == 0:
            curr_lr = optimizer.param_groups[0]['lr']
            curr_line = "Epoch [{}/{}], Step [{}/{}] Loss: {:.8f}, LR: {:.8f}\n".format(
                epoch+1, args.no_epochs, i+1, steps_per_epoch, loss.item(), curr_lr)
            utilities.misc.print_write(f, curr_line)
            wandb.log({'Training loss (step)': loss.item(),
                'Learning rate (current)': curr_lr})

            if masks is not None:
                utilities.misc.decode_text(f, tokenizer, outputs_text, captions, labels_text)

        if args.debugging and ((i + 1) % (args.log_freq * 3) == 0):
            break    

    # Decay learning rate
    if not lr_scheduler:
        if (epoch+1) % args.epoch_decay == 0:
            utilities.misc.update_lr(optimizer)

    # calculates mean of losses for current epoch and appends to list of avgs
    train_loss_avg.append(mean(current_losses))
    wandb.log({'Training loss (epoch)': mean(current_losses)}) 


def validate(args, f, global_step, model, device, tokenizer, loader,
    mask_scheduler, top1_accuracies, top5_accuracies, val_loss_avg=[]):
    # Test the model (validation set)
    # eval mode (batchnorm uses moving mean/variance instead of mini-batch mean/variance)
    # dropout probability goes to 0
    criterion = torch.nn.CrossEntropyLoss()
    if args.mask_schedule:
        criterion_mlm = torch.nn.CrossEntropyLoss(reduction='none')
    
    model.eval()
    with torch.no_grad():
        correct_1 = 0
        correct_5 = 0
        total = 0
        current_losses = []
        steps_per_epoch = len(loader)
        
        for i, batch in enumerate(loader):
            if args.multimodal:
                images, labels, captions = batch
                captions = captions.squeeze(dim=1).to(device)
            else:
                images, labels = batch
            images = images.to(device)
            labels = labels.to(device)
                
            masks = mask_scheduler.ret_mask(global_step)
            if masks is not None:
                # 0 is [PAD], 101 is [CLS], 102 is [SEP]
                labels_text = torch.where((captions==0) | (captions==101) | (captions==102), -100, captions)
                labels_text = labels_text.to(device)
                masks = masks.to(device)                
                labels_text = torch.where(masks[:, -args.max_text_seq_len:]==1, -100, labels_text)
                        
            # Forward pass
            if args.multimodal and args.mask_schedule and args.exclusion_loss:
                outputs, outputs_text, exclusion_loss = model(images, text=captions, mask=masks)
            elif args.multimodal and args.mask_schedule:
                outputs, outputs_text = model(images, text=captions, mask=masks)
            elif args.multimodal and args.exclusion_loss:
                outputs, exclusion_loss = model(images, text=captions, mask=masks)
            elif args.multimodal:
                outputs = model(images, text=captions, mask=masks)
            elif args.exclusion_loss:
                outputs, exclusion_loss = model(images)
            else:
                outputs = model(images)
                
            loss = criterion(outputs, labels)
            if masks is not None:
                loss = loss + criterion(outputs_text.transpose(1, 2), labels_text)
            if args.exclusion_loss:
                loss =  loss - (args.exclusion_weight * (args.temperature ** 2) * exclusion_loss)
            
            current_losses.append(loss.item())
            
            # calculate top-k (1 and 5) accuracy
            total += labels.size(0)
            curr_corr_list = utilities.misc.accuracy(outputs.data, labels, (1, 5, ))
            correct_1 += curr_corr_list[0]
            correct_5 += curr_corr_list[1]

            if i % args.log_freq == 0:
                curr_line = "Validation/Test Step [{}/{}] Loss: {:.8f}\n".format(
                i+1, steps_per_epoch, loss.item())
                utilities.misc.print_write(f, curr_line)

                if masks is not None:
                    utilities.misc.decode_text(f, tokenizer, outputs_text, captions, labels_text)

            if args.debugging and ((i + 1) % (args.log_freq * 3) == 0):
                break    
            
        # append avg val loss
        val_loss_avg.append(mean(current_losses))

        # compute epoch accuracy in percentages
        curr_top1_acc = 100 * correct_1/total
        top1_accuracies.append(curr_top1_acc)
        curr_line = 'Val/Test Top-1 Accuracy of the model on the test images: {:.4f} %'.format(curr_top1_acc)
        utilities.misc.print_write(f, curr_line)
        
        curr_top5_acc = 100 * correct_5/total
        top5_accuracies.append(curr_top5_acc)
        curr_line = 'Val/Test Top-5 Accuracy of the model on the test images: {:.4f} %'.format(curr_top5_acc)
        utilities.misc.print_write(f, curr_line)

        wandb.log({"Epoch": len(top1_accuracies),
        "Val Accuracy Top-1": curr_top1_acc, 
        "Val Accuracy Top-5": curr_top5_acc,
        "Val Loss": mean(current_losses)})

        return curr_top1_acc


def train_main(logger, args):

    time_start = time.time()

    (f, device, train_set, train_loader, val_loader, test_loader, 
    classid_classname_dic, model, optimizer, lr_scheduler, 
    mask_scheduler, tokenizer) = environment_loader(args)
    
    # Train the model
    train_loss_avg = []
    val_loss_avg = []
    top1_accuracies = []
    top5_accuracies = []
    best_epoch = 0
    curr_acc = 0
    top_acc = 0
    max_memory = 0
    global_step = [0]
    
    for epoch in range(args.no_epochs):
        time_start_epoch = time.time()

        train_one_epoch(args, f, epoch, global_step, model, device, tokenizer,
        optimizer, mask_scheduler, lr_scheduler, train_loader, train_loss_avg)

        curr_max_memory = torch.cuda.max_memory_reserved()/(1024**3)
        if max_memory < curr_max_memory:
            max_memory = curr_max_memory
        
        # validates on test set once per epoch, calculates top1/5 acc and val loss avg
        curr_acc = validate(args, f, global_step, model=model, device=device, tokenizer=tokenizer, loader=val_loader,
        mask_scheduler=mask_scheduler, top1_accuracies=top1_accuracies, top5_accuracies=top5_accuracies, 
        val_loss_avg=val_loss_avg)

        # Save the model checkpoint if the top1-acc is higher than current highest
        if curr_acc > top_acc:
            torch.save(model.state_dict(), os.path.join(args.results_dir, 
            '{}.ckpt'.format(args.run_name)))
            top_acc = curr_acc
            best_epoch = epoch + 1
        
    # validate on test set and plot results
    validate(args, f, global_step, model=model, device=device, tokenizer=tokenizer, loader=test_loader, 
    mask_scheduler=mask_scheduler, top1_accuracies=top1_accuracies, top5_accuracies=top5_accuracies)

    time_end = time.time()
    time_all = time_end - time_start

    utilities.misc.log_summary_stats(args, logger, f, top_acc, best_epoch, max_memory,
    time_all, top1_accuracies, top5_accuracies, train_loss_avg, val_loss_avg)


def main():

    logging.basicConfig(filename='logs.txt', level=logging.INFO, 
    format='%(asctime)s - %(levelname)s - %(name)s - %(message)s',
    datefmt='%m/%d/%Y %H:%M:%S',)
    
    args = utilities.misc.ret_args()

    logger.info(args)

    os.makedirs(args.results_dir, exist_ok=True)

    train_main(logger, args)            

if __name__ == '__main__':
    main()
