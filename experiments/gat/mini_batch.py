import argparse
from timeit import default_timer
from typing import Callable, Union

import dgl
import sigopt
import torch
import torch.nn as nn
import torch.nn.functional as F
from ogb.nodeproppred import Evaluator

import utils
from model import GAT


def train(
    model: nn.Module,
    device: Union[str, torch.device],
    optimizer: torch.optim.Optimizer,
    loss_function: Callable[[torch.Tensor, torch.Tensor], torch.Tensor],
    evaluator: Evaluator,
    dataloader: dgl.dataloading.NodeDataLoader,
) -> tuple[float]:
    model.train()

    total_loss = 0
    total_score = 0

    start = default_timer()

    for step, (_, _, blocks) in enumerate(dataloader):
        optimizer.zero_grad()

        blocks = [block.int().to(device) for block in blocks]

        inputs = blocks[0].srcdata['feat']
        labels = blocks[-1].dstdata['label']

        logits = model(blocks, inputs)

        loss = loss_function(logits, labels)
        score = utils.get_evaluation_score(evaluator, logits, labels)

        loss.backward()
        optimizer.step()

        total_loss += loss.item()
        total_score += score

    stop = default_timer()
    time = stop - start

    total_loss /= step + 1
    total_score /= step + 1

    return time, total_loss, total_score


def validate(
    model: nn.Module,
    loss_function: Callable[[torch.Tensor, torch.Tensor], torch.Tensor],
    evaluator: Evaluator,
    g: dgl.DGLGraph,
    mask: torch.Tensor,
) -> tuple[float]:
    model.eval()

    start = default_timer()

    inputs = g.ndata['feat']
    labels = g.ndata['label'][mask]

    with torch.no_grad():
        logits = model(g, inputs)[mask]

        loss = loss_function(logits, labels)
        score = utils.get_evaluation_score(evaluator, logits, labels)

    stop = default_timer()
    time = stop - start

    loss = loss.item()

    return time, loss, score


def log_run(args: argparse.ArgumentParser, sigopt_run: sigopt.LiveRunContext = None) -> None:
    torch.manual_seed(args.seed)

    dataset, evaluator, g, train_idx, valid_idx, test_idx = utils.process_dataset(
        args.dataset,
        root=args.dataset_root,
        reverse_edges=args.graph_reverse_edges,
        self_loop=args.graph_self_loop,
    )

    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

    run.params.setdefaults({
        'lr': args.lr,
        'node_hidden_feats': args.node_hidden_feats,
        'num_heads': args.num_heads,
        'num_layers': args.num_layers,
        'norm': args.norm,
        'batch_norm': int(args.batch_norm),
        'input_dropout': args.input_dropout,
        'attn_dropout': args.attn_dropout,
        'edge_dropout': args.edge_dropout,
        'dropout': args.dropout,
        'negative_slope': args.negative_slope,
        'residual': int(args.residual),
        'activation': args.activation,
        'use_attn_dst': int(args.use_attn_dst),
        'bias': int(args.bias),
        'batch_size': args.batch_size,
    })

    fanouts = utils.set_sigopt_fanouts(args.fanouts)

    sampler = dgl.dataloading.MultiLayerNeighborSampler(fanouts=fanouts)
    train_dataloader = dgl.dataloading.NodeDataLoader(
        g,
        train_idx,
        sampler,
        batch_size=run.params.batch_size,
        shuffle=True,
        drop_last=False,
        num_workers=4,
    )

    node_in_feats = g.ndata['feat'].shape[-1]

    if args.dataset == 'ogbn-proteins':
        if args.edge_hidden_feats > 0:
            run.params.setdefaults(
                {'edge_hidden_feats': args.edge_hidden_feats})
        else:
            run.params.setdefaults({'edge_hidden_feats': 16})

        edge_in_feats = g.edata['feat'].shape[-1]
        edge_hidden_feats = run.params.edge_hidden_feats
    else:
        edge_in_feats = 0
        edge_hidden_feats = 0

    out_feats = dataset.num_classes

    activations = {'leaky_relu': F.leaky_relu, 'relu': F.relu}

    model = GAT(
        node_in_feats,
        edge_in_feats,
        run.params.node_hidden_feats,
        edge_hidden_feats,
        out_feats,
        run.params.num_heads,
        run.params.num_layers,
        norm=run.params.norm,
        batch_norm=bool(run.params.batch_norm),
        input_dropout=run.params.input_dropout,
        attn_dropout=run.params.attn_dropout,
        edge_dropout=run.params.edge_dropout,
        dropout=run.params.dropout,
        negative_slope=run.params.negative_slope,
        residual=bool(run.params.residual),
        activation=activations[run.params.activation],
        use_attn_dst=bool(run.params.use_attn_dst),
        bias=bool(run.params.bias),
    ).to(device)

    if args.dataset == 'ogbn-proteins':
        loss_function = nn.BCEWithLogitsLoss().to(device)
    else:
        loss_function = nn.CrossEntropyLoss().to(device)

    optimizer = torch.optim.Adam(model.parameters(), lr=run.params.lr)

    checkpoint = utils.Callback(args.early_stopping_patience,
                                args.early_stopping_monitor)

    for epoch in range(args.num_epochs):
        train_time, train_loss, train_score = train(
            model, device, optimizer, loss_function, evaluator, train_dataloader)
        valid_time, valid_loss, valid_score = validate(
            model, loss_function, evaluator, g, valid_idx)

        checkpoint.create(
            epoch,
            train_time,
            valid_time,
            train_loss,
            valid_loss,
            train_score,
            valid_score,
            model,
        )

        print(
            f'Epoch: {epoch + 1:03} '
            f'Train Loss: {train_loss:.2f} '
            f'Valid Loss: {valid_loss:.2f} '
            f'Train Score: {train_score:.4f} '
            f'Valid Score: {valid_score:.4f} '
            f'Train Epoch Time: {train_time:.2f} '
            f'Valid Epoch Time: {valid_time:.2f}'
        )

        if checkpoint.should_stop:
            print('!! Early Stopping !!')

            break

    if args.test_validation:
        model.load_state_dict(checkpoint.best_epoch_model_parameters)

        test_time, test_loss, test_score = validate(
            model, loss_function, evaluator, g, test_idx)

        print(
            f'Test Loss: {test_loss:.2f} '
            f'Test Score: {test_score:.4f} % '
            f'Test Epoch Time: {test_time:.2f}'
        )

        utils.log_metrics_to_sigopt(
            checkpoint,
            'GAT NS',
            args.dataset,
            test_loss,
            test_score,
            test_time,
        )
    else:
        utils.log_metrics_to_sigopt(checkpoint, 'GAT NS', args.dataset)


if __name__ == '__main__':
    argparser = argparse.ArgumentParser('GAT NS Optimization')

    argparser.add_argument('--dataset', default='ogbn-products', type=str,
                           choices=['ogbn-arxiv', 'ogbn-products', 'ogbn-proteins'])
    argparser.add_argument('--dataset-root', default='dataset', type=str)
    argparser.add_argument('--download-dataset', default=False,
                           action=argparse.BooleanOptionalAction)
    argparser.add_argument('--create-experiment', default=False,
                           action=argparse.BooleanOptionalAction)
    argparser.add_argument('--experiment-id', default=None, type=int)
    argparser.add_argument('--graph-reverse-edges', default=False,
                           action=argparse.BooleanOptionalAction)
    argparser.add_argument('--graph-self-loop', default=False,
                           action=argparse.BooleanOptionalAction)
    argparser.add_argument('--num-epochs', default=500, type=int)
    argparser.add_argument('--lr', default=0.001, type=float)
    argparser.add_argument('--node-hidden-feats', default=128, type=int)
    argparser.add_argument('--edge-hidden-feats', default=0, type=int)
    argparser.add_argument('--num-heads', default=4, type=int)
    argparser.add_argument('--num-layers', default=3, type=int)
    argparser.add_argument('--norm', default='none',
                           type=str, choices=['both', 'left', 'none', 'right'])
    argparser.add_argument('--batch-norm', default=False,
                           action=argparse.BooleanOptionalAction)
    argparser.add_argument('--input-dropout', default=0, type=float)
    argparser.add_argument('--attn-dropout', default=0, type=float)
    argparser.add_argument('--edge-dropout', default=0, type=float)
    argparser.add_argument('--dropout', default=0, type=float)
    argparser.add_argument('--negative-slope', default=0.2, type=float)
    argparser.add_argument('--residual', default=False,
                           action=argparse.BooleanOptionalAction)
    argparser.add_argument('--activation', default='relu',
                           type=str, choices=['leaky_relu', 'relu'])
    argparser.add_argument('--use-attn-dst', default=True,
                           action=argparse.BooleanOptionalAction)
    argparser.add_argument('--bias', default=True,
                           action=argparse.BooleanOptionalAction)
    argparser.add_argument('--batch-size', default=512, type=int)
    argparser.add_argument('--fanouts', default='10,10,10', type=str)
    argparser.add_argument('--early-stopping-patience', default=10, type=int)
    argparser.add_argument('--early-stopping-monitor',
                           default='loss', type=str)
    argparser.add_argument('--test-validation', default=True,
                           action=argparse.BooleanOptionalAction)
    argparser.add_argument('--seed', default=13, type=int)

    args = argparser.parse_args()

    if args.download_dataset:
        utils.download_dataset(args.dataset)

    if args.create_experiment:
        import yaml
        exp_meta = yaml.load(
            open('./mini_batch_experiment.yml'), Loader=yaml.FullLoader)
        experiment = sigopt.create_experiment(**exp_meta)
    elif args.experiment_id:
        experiment = sigopt.get_experiment(args.experiment_id)
    else:
        print("No experiment ID given and not creating experiment")
        exit

    while not experiment.is_finished():
        with experiment.create_run() as run:
            log_run(args)
        experiment = sigopt.get_experiment(args.experiment_id)
