import argparse
import os
from timeit import default_timer
from typing import Callable

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
    optimizer: torch.optim.Optimizer,
    loss_function: Callable[[torch.Tensor, torch.Tensor], torch.Tensor],
    evaluator: Evaluator,
    g: dgl.DGLGraph,
    mask: torch.Tensor,
) -> tuple[float]:
    model.train()
    optimizer.zero_grad()

    start = default_timer()

    inputs = g.ndata['feat']
    labels = g.ndata['label'][mask]

    logits = model(g, inputs)[mask]

    loss = loss_function(logits, labels)
    score = utils.get_evaluation_score(evaluator, logits, labels)

    loss.backward()
    optimizer.step()

    stop = default_timer()
    time = stop - start

    loss = loss.item()

    return time, loss, score

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

def run(args: argparse.ArgumentParser, experiment=None) -> None:

    torch.manual_seed(args.seed)

    dataset, evaluator, g, train_idx, valid_idx, test_idx = utils.process_dataset(
        args.dataset,
        root=args.dataset_root,
        reverse_edges=args.graph_reverse_edges,
        self_loop=args.graph_self_loop,
    )

    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

    if experiment is not None:
        suggestion = experiment.suggestions().create()
        assignments = suggestion.assignments
        lr = assignments['lr']
        node_hidden_feats = assignments['node_hidden_feats']
        num_heads = assignments['num_heads']
        num_layers = assignments['num_layers']
        norm = assignments['norm']
        batch_norm = bool(assignments['batch_norm'])
        input_dropout = assignments['input_dropout']
        attn_dropout = assignments['attn_dropout']
        edge_dropout = assignments['edge_dropout']
        dropout = assignments['dropout'] 
        negative_slope = assignments['negative_slope']
        residual = bool(assignments['residual'])
        activation = assignments['activation']
        use_attn_dst = bool(assignments['use_attn_dst'])
        bias = bool(assignments['bias'])
    else:
        lr = args.lr
        node_hidden_feats = args.node_hidden_feats
        num_heads = args.num_heads
        num_layers = args.num_layers
        norm = args.norm
        batch_norm = int(args.batch_norm)
        input_dropout = args.input_dropout
        attn_dropout = args.attn_dropout
        edge_dropout = args.edge_dropout
        dropout = args.dropout 
        negative_slope = args.negative_slope
        residual = int(args.residual)
        activation = args.activation
        use_attn_dst = int(args.use_attn_dst)
        bias = int(args.bias)
        
    node_in_feats = g.ndata['feat'].shape[-1]

    if args.dataset == 'ogbn-proteins':
        if args.edge_hidden_feats > 0:
            # run.params.setdefaults(
            #     {'edge_hidden_feats': args.edge_hidden_feats})
            edge_hidden_feats = assignments['edge_hidden_feats']
        else:
            #run.params.setdefaults({'edge_hidden_feats': 16})
            edge_hidden_feats = 16

        edge_in_feats = g.edata['feat'].shape[-1]
        #edge_hidden_feats = run.params.edge_hidden_feats
    else:
        edge_in_feats = 0
        edge_hidden_feats = 0

    out_feats = dataset.num_classes

    activations = {'leaky_relu': F.leaky_relu, 'relu': F.relu}

    model = GAT(
        node_in_feats,
        edge_in_feats,
        node_hidden_feats,
        edge_hidden_feats,
        out_feats,
        num_heads,
        num_layers,
        norm=norm,
        batch_norm=batch_norm,
        input_dropout=input_dropout,
        attn_dropout=attn_dropout,
        edge_dropout=edge_dropout,
        dropout=dropout,
        negative_slope=negative_slope,
        residual=residual,
        activation=activations[activation],
        use_attn_dst=use_attn_dst,
        bias=bias,
    ).to(device)

    if args.dataset == 'ogbn-proteins':
        loss_function = nn.BCEWithLogitsLoss().to(device)
    else:
        loss_function = nn.CrossEntropyLoss().to(device)

    optimizer = torch.optim.Adam(model.parameters(), lr=lr)

    checkpoint = utils.Callback(args.early_stopping_patience,
                                args.early_stopping_monitor)

    for epoch in range(args.num_epochs):
        train_time, train_loss, train_score = train(
            model, optimizer, loss_function, evaluator, g, train_idx)
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
            experiment,
            suggestion,
            checkpoint,
            'GAT',
            args.dataset,
            test_loss,
            test_score,
            test_time,
        )
    else:
        utils.log_metrics_to_sigopt(
            experiment,
            suggestion,
            checkpoint, 
            'GAT NS', 
            args.dataset
        )

if __name__ == '__main__':
    argparser = argparse.ArgumentParser('GAT NS Optimization')

    argparser.add_argument('--dataset', default='ogbn-products', type=str,
                           choices=['ogbn-arxiv', 'ogbn-products', 'ogbn-proteins'])
    argparser.add_argument('--dataset-root', default='dataset', type=str)
    argparser.add_argument('--download-dataset', default=False,
                           action=argparse.BooleanOptionalAction)
    argparser.add_argument('--sigopt-api-token', default=None, type=str)
    argparser.add_argument('--experiment-id', default=None, type=str)
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
    argparser.add_argument('--early-stopping-patience', default=10, type=int)
    argparser.add_argument('--early-stopping-monitor',
                           default='loss', type=str)
    argparser.add_argument('--test-validation', default=True,
                           action=argparse.BooleanOptionalAction)
    argparser.add_argument('--seed', default=13, type=int)

    args = argparser.parse_args()

    if args.download_dataset:
        utils.download_dataset(args.dataset)

    if args.experiment_id is not None:
        if args.sigopt_api_token is not None:
            token = args.sigopt_api_token
        else:
            token = os.getenv('SIGOPT_API_TOKEN')

            if token is None:
                raise ValueError(
                    'SigOpt API token is not provided. Please provide it by '
                    '--sigopt-api-token argument or set '
                    'SIGOPT_API_TOKEN environment variable.'
                )

        experiment = sigopt.Connection(token).experiments(args.experiment_id)

        while utils.is_experiment_finished(experiment):
            run(args, experiment)
    else:
        run(args)
