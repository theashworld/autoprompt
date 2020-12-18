"""
Script for running finetuning on glue tasks.

Largely copied from:
    https://github.com/huggingface/transformers/blob/master/examples/text-classification/run_glue.py
"""
import argparse
import logging
from pathlib import Path
import random
import shutil

import numpy as np
import torch
import torch.nn.functional as F
from torch.utils.data import DataLoader
from torch.optim.lr_scheduler import LambdaLR
import transformers
from transformers import (
    AdamW, AutoConfig, AutoModelForSequenceClassification, AutoTokenizer
)
from tqdm import tqdm

import autoprompt.utils as utils
from autoprompt.preprocessors import PREPROCESSORS


logger = logging.getLogger(__name__)


def set_seed(seed: int):
    """Sets the relevant random seeds."""
    random.seed(seed)
    np.random.seed(seed)
    torch.random.manual_seed(seed)
    torch.cuda.manual_seed(seed)


def get_linear_schedule_with_warmup(optimizer, num_warmup_steps, num_training_steps, last_epoch=-1):
    """ Create a schedule with a learning rate that decreases linearly after
    linearly increasing during a warmup period.

    From:
        https://github.com/uds-lsv/bert-stable-fine-tuning/blob/master/src/transformers/optimization.py
    """

    def lr_lambda(current_step):
        if current_step < num_warmup_steps:
            return float(current_step) / float(max(1, num_warmup_steps))
        return max(
            0.0, float(num_training_steps - current_step) / float(max(1, num_training_steps - num_warmup_steps))
        )

    return LambdaLR(optimizer, lr_lambda, last_epoch)


def main(args):
    set_seed(args.seed)

    if args.rank == -1:
        device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    else:
        device = torch.device('cuda', args.rank)

    config = AutoConfig.from_pretrained(args.model_name, num_labels=args.num_labels)
    tokenizer = AutoTokenizer.from_pretrained(args.model_name)
    if args.adapter:
        model = transformers.AutoModelWithHeads.from_pretrained(
            args.model_name,
            config=config,
        )
        model.add_adapter(
            'adapter', 
            transformers.AdapterType.text_task,
            config='pfeiffer',
        )
        model.train_adapter(['adapter'])
        model.add_classification_head('adapter', num_labels=args.num_labels)
        model.set_active_adapters('adapter')
    else:
        model = AutoModelForSequenceClassification.from_pretrained(
            args.model_name,
            config=config,
        )
    model.to(device)


    collator = utils.Collator(pad_token_id=tokenizer.pad_token_id)
    train_dataset, label_map = utils.load_classification_dataset(
        args.train,
        tokenizer,
        args.field_a,
        args.field_b,
        args.label_field,
        limit=args.limit,
        preprocessor_key=args.preprocessor,
    )
    train_loader = DataLoader(train_dataset, batch_size=args.bsz, shuffle=True, collate_fn=collator)
    dev_dataset, _ = utils.load_classification_dataset(
        args.dev,
        tokenizer,
        args.field_a,
        args.field_b,
        args.label_field,
        label_map,
        limit=args.limit,
        preprocessor_key=args.preprocessor,
    )
    dev_loader = DataLoader(dev_dataset, batch_size=args.bsz, shuffle=False, collate_fn=collator)
    test_dataset, _ = utils.load_classification_dataset(
        args.test,
        tokenizer,
        args.field_a,
        args.field_b,
        args.label_field,
        label_map,
        preprocessor_key=args.preprocessor,
    )
    test_loader = DataLoader(test_dataset, batch_size=args.bsz, shuffle=False, collate_fn=collator)

    if args.bias_correction:
        betas = (0.9, 0.999)
    else:
        betas = (0.0, 0.000)

    optimizer = AdamW(
        model.parameters(),
        lr=args.lr,
        weight_decay=1e-2,
        betas=betas
    )
    scaler = torch.cuda.amp.GradScaler(enabled=args.fp16)

    # Use suggested learning rate scheduler
    num_training_steps = len(train_dataset) * args.epochs // args.bsz
    num_warmup_steps = num_training_steps // 10
    scheduler = get_linear_schedule_with_warmup(optimizer, num_warmup_steps,
                                                num_training_steps)

    if not args.ckpt_dir.exists():
        logger.info(f'Making checkpoint directory: {args.ckpt_dir}')
        args.ckpt_dir.mkdir(parents=True)
    elif not args.force_overwrite:
        raise RuntimeError('Checkpoint directory already exists.')

    try:
        best_accuracy = 0
        for epoch in range(args.epochs):
            logger.info('Training...')
            model.train()
            avg_loss = utils.ExponentialMovingAverage()
            pbar = tqdm(train_loader)
            for model_inputs, labels in pbar:
                model_inputs = {k: v.to(device) for k, v in model_inputs.items()}
                labels = labels.to(device)
                with torch.cuda.amp.autocast():
                    logits, *_ = model(**model_inputs)
                    loss = F.cross_entropy(logits, labels.squeeze(-1))
                scaler.scale(loss).backward()
                scaler.step(optimizer)
                scaler.update()
                optimizer.zero_grad()
                scheduler.step()
                avg_loss.update(loss.item())
                pbar.set_description(f'loss: {avg_loss.get_metric(): 0.4f}, '
                                     f'lr: {optimizer.param_groups[0]["lr"]: .3e}')

            logger.info('Evaluating...')
            model.eval()
            correct = 0
            total = 0
            with torch.no_grad():
                for model_inputs, labels in dev_loader:
                    model_inputs = {k: v.to(device) for k, v in model_inputs.items()}
                    labels = labels.to(device)
                    logits, *_ = model(**model_inputs)
                    _, preds = logits.max(dim=-1)
                    correct += (preds == labels.squeeze(-1)).sum().item()
                    total += labels.size(0)
                accuracy = correct / (total + 1e-13)
            logger.info(f'Accuracy: {accuracy : 0.4f}')

            if accuracy > best_accuracy:
                logger.info('Best performance so far.')
                if not args.adapter:
                    model.save_pretrained(args.ckpt_dir)
                tokenizer.save_pretrained(args.ckpt_dir)
                best_accuracy = accuracy
    except KeyboardInterrupt:
        logger.info('Interrupted...')

    logger.info('Testing...')
    if not args.adapter:
        model.from_pretrained(args.ckpt_dir)
    model.eval()
    shutil.rmtree(args.ckpt_dir)
    correct = 0
    total = 0

    with torch.no_grad():
        for model_inputs, labels in test_loader:
            model_inputs = {k: v.to(device) for k, v in model_inputs.items()}
            labels = labels.to(device)
            logits, *_ = model(**model_inputs)
            _, preds = logits.max(dim=-1)
            correct += (preds == labels.squeeze(-1)).sum().item()
            total += labels.size(0)
        accuracy = correct / (total + 1e-13)
    logger.info(f'Accuracy: {accuracy : 0.4f}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--model-name', type=str)
    parser.add_argument('--train', type=Path)
    parser.add_argument('--dev', type=Path)
    parser.add_argument('--test', type=Path)
    parser.add_argument('--field-a', type=str)
    parser.add_argument('--field-b', type=str, default=None)
    parser.add_argument('--label-field', type=str, default='label')
    parser.add_argument('--preprocessor', type=str, default=None,
                        choices=PREPROCESSORS.keys())
    parser.add_argument('--ckpt-dir', type=Path, default=Path('ckpt/'))
    parser.add_argument('--num-labels', type=int, default=2)
    parser.add_argument('--bsz', type=int, default=32)
    parser.add_argument('--epochs', type=int, default=3)
    parser.add_argument('--lr', type=float, default=2e-5)
    parser.add_argument('--limit', type=int, default=None)
    parser.add_argument('--seed', type=int, default=1234)
    parser.add_argument('--bias-correction', action='store_true')
    parser.add_argument('--fp16', action='store_true')
    parser.add_argument('-f', '--force-overwrite', action='store_true')
    parser.add_argument('--adapter', action='store_true')
    parser.add_argument('--debug', action='store_true')
    parser.add_argument('--rank', type=int, default=-1)
    args = parser.parse_args()

    if args.debug:
        level = logging.DEBUG
    else:
        level = logging.INFO
    logging.basicConfig(level=level)

    main(args)

