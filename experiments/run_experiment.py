import argparse
import os


def run_experiment(args: argparse.ArgumentParser):
    path = f'{args.model}/{args.training_method.replace("-", "_")}.py'

    arguments = [f'--dataset {args.dataset}']

    if args.sigopt_api_token is not None:
        arguments.append(f'--sigopt-api-token {args.sigopt_api_token}')

    if args.experiment_id is not None:
        arguments.append(f'--experiment-id {args.experiment_id}')

    if args.dataset_root is not None:
        arguments.append(f'--dataset-root {args.dataset_root}')

    if args.optimization_target == 'speed':
        arguments.append('--test-validation')
    elif args.optimization_target == 'accuracy':
        arguments.append('--no-test-validation')

    arguments = ' '.join(arguments)

    os.system(f'OMP_NUM_THREADS=20 python {path} {arguments}')


if __name__ == '__main__':
    argparser = argparse.ArgumentParser('Create SigOpt experiment')

    argparser.add_argument('--sigopt-api-token', default=None, type=str)
    argparser.add_argument('--experiment-id', default=None, type=str)
    argparser.add_argument('--model', type=str,
                           choices=['gat', 'graphsage', 'rgcn'])
    argparser.add_argument('--dataset', type=str,
                           choices=['ogbn-arxiv', 'ogbn-mag', 'ogbn-products', 'ogbn-proteins'])
    argparser.add_argument('--dataset-root', default=None, type=str)
    argparser.add_argument('--training-method', type=str,
                           choices=['mini-batch', 'full-graph'])
    argparser.add_argument('--optimization-target', default=None, type=str,
                           choices=['accuracy', 'speed'])

    args = argparser.parse_args()

    run_experiment(args)
