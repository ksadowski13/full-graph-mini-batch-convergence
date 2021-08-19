from collections.abc import Callable
from timeit import default_timer
from typing import Union

import dgl
import dgl.function as fn
import torch
import torch.nn.functional as F
import torch.nn as nn
from dgl.ops import edge_softmax
from dgl.utils import expand_as_pair


class GATConv(nn.Module):
    def __init__(
        self,
        node_in_feats: int,
        edge_in_feats: int,
        out_feats: int,
        num_heads: int,
        norm: str = 'none',
        attn_dropout: float = 0,
        edge_dropout: float = 0,
        negative_slope: float = 0.2,
        residual: bool = True,
        activation: Callable[[torch.Tensor], torch.Tensor] = None,
        use_attn_dst: bool = True,
        allow_zero_in_degree: bool = True,
    ):
        super().__init__()
        self._out_feats = out_feats
        self._num_heads = num_heads
        self._norm = norm
        self._attn_dropout = nn.Dropout(attn_dropout)
        self._edge_dropout = edge_dropout
        self._leaky_relu = nn.LeakyReLU(negative_slope)
        self._activation = activation
        self._allow_zero_in_degree = allow_zero_in_degree
        self._in_src_feats, self._in_dst_feats = expand_as_pair(node_in_feats)

        self._fc_src = nn.Linear(
            self._in_src_feats, out_feats * num_heads, bias=False)

        if residual:
            self._fc_dst = nn.Linear(
                self._in_dst_feats, out_feats * num_heads, bias=False)
            self.bias = None
        else:
            self._fc_dst = None
            self.bias = nn.Parameter(out_feats * num_heads)

        self._attn_fc_src = nn.Linear(
            self._in_src_feats, num_heads, bias=False)

        if use_attn_dst:
            self._attn_fc_dst = nn.Linear(
                self._in_src_feats, num_heads, bias=False)
        else:
            self._attn_fc_dst = None

        if edge_in_feats > 0:
            self._attn_fc_edge = nn.Linear(
                edge_in_feats, num_heads, bias=False)
        else:
            self._attn_fc_edge = None

    def reset_parameters(self):
        gain = nn.init.calculate_gain('relu')

        nn.init.xavier_normal_(self._fc_src.weight, gain=gain)

        if self._fc_dst is not None:
            nn.init.xavier_normal_(self._fc_dst.weight, gain=gain)

        nn.init.xavier_normal_(self._attn_fc_src.weight, gain=gain)

        if self._attn_fc_dst is not None:
            nn.init.xavier_normal_(self._attn_fc_dst.weight, gain=gain)

        if self._attn_fc_edge is not None:
            nn.init.xavier_normal_(self._attn_fc_edge.weight, gain=gain)

        if self.bias is not None:
            nn.init.zeros_(self.bias)

    def set_allow_zero_in_degree(self, value: bool):
        self._allow_zero_in_degree = value

    def forward(
        self,
        g: dgl.DGLGraph,
        node_inputs: torch.Tensor,
        edge_inputs: torch.Tensor = None,
    ) -> torch.Tensor:
        with g.local_scope():
            if not self._allow_zero_in_degree:
                if (g.in_degrees() == 0).any():
                    assert False

        if g.is_block:
            node_inputs_dst = node_inputs[:g.num_dst_nodes()]
        else:
            node_inputs_dst = node_inputs

        feat_fc_src = self._fc_src(node_inputs).view(
            -1, self._num_heads, self._out_feats)
        feat_fc_dst = self._fc_dst(node_inputs_dst).view(
            -1, self._num_heads, self._out_feats)

        if self._norm in ['both', 'left']:
            degrees = g.out_degrees().float().clamp(min=1)

            if self._norm == 'both':
                norm = torch.pow(degrees, -0.5)
            else:
                norm = 1 / degrees

            shape = norm.shape + (1,) * (feat_fc_src.dim() - 1)
            norm = torch.reshape(norm, shape)

            feat_fc_src *= norm

        attn_src = self._attn_fc_src(node_inputs).view(
            -1, self._num_heads, 1)

        g.srcdata.update({'feat_fc_src': feat_fc_src, 'attn_src': attn_src})

        if self._attn_fc_dst is not None:
            attn_dst = self._attn_fc_dst(
                node_inputs_dst).view(-1, self._num_heads, 1)

            g.dstdata.update({'attn_dst': attn_dst})
            g.apply_edges(fn.u_add_v('attn_src', 'attn_dst', 'attn_node'))
        else:
            g.apply_edges(fn.copy_u('attn_src', 'attn_node'))

        e = g.edata['attn_node']

        if edge_inputs is not None:
            attn_edge = self._attn_fc_edge(edge_inputs).view(
                -1, self._num_heads, 1)

            g.edata.update({'attn_edge': attn_edge})
            e += g.edata['attn_edge']

        e = self._leaky_relu(e)

        if self.training and self._edge_dropout > 0:
            perm = torch.randperm(g.num_edges(), device=e.device)
            bound = int(g.num_edges() * self._edge_dropout)
            eids = perm[bound:]

            g.edata['attn'] = torch.zeros_like(e)
            g.edata['attn'][eids] = self._attn_dropout(
                edge_softmax(g, e[eids], eids=eids))
        else:
            g.edata['attn'] = self._attn_dropout(edge_softmax(g, e))

        g.update_all(fn.u_mul_e('feat_fc_src', 'attn', 'msg'),
                     fn.sum('msg', 'feat_fc_src'))
        x = g.dstdata['feat_fc_src']

        if self._norm in ['both', 'right']:
            degrees = g.in_degrees().float().clamp(min=1)

            if self._norm == 'both':
                norm = torch.pow(degrees, -0.5)
            else:
                norm = 1 / degrees

            shape = norm.shape + (1,) * (feat_fc_dst.dim() - 1)
            norm = torch.reshape(norm, shape)

            x *= norm

        if self._fc_dst is not None:
            x += feat_fc_dst
        else:
            x += self.bias

        if self._activation is not None:
            x = self._activation(x)

        return x


class GAT(nn.Module):
    def __init__(
        self,
        node_in_feats: int,
        edge_in_feats: int,
        node_hidden_feats: int,
        edge_hidden_feats: int,
        out_feats: int,
        num_heads: int,
        num_layers: int,
        norm: str = 'none',
        batch_norm: bool = False,
        input_dropout: float = 0,
        attn_dropout: float = 0,
        edge_dropout: float = 0,
        dropout: float = 0,
        negative_slope: float = 0.2,
        residual: bool = True,
        activation: Callable[[torch.Tensor], torch.Tensor] = None,
        use_attn_dst: bool = True,
        allow_zero_in_degree: bool = True,
    ):
        super().__init__()
        self._num_heads = num_heads
        self._num_layers = num_layers
        self._input_dropout = nn.Dropout(input_dropout)
        self._dropout = nn.Dropout(dropout)
        self._residual = residual
        self._activation = activation

        self._node_encoder = nn.Linear(node_in_feats, node_hidden_feats)

        if edge_hidden_feats > 0:
            self._edge_encoder = nn.ModuleList()
        else:
            self._edge_encoder = None

        self._convs = nn.ModuleList()

        if batch_norm:
            self._batch_norms = nn.ModuleList()
        else:
            self._batch_norms = None

        for i in range(num_layers):
            in_hidden = num_heads * node_hidden_feats if i > 0 else node_hidden_feats
            out_hidden = node_hidden_feats

            if self._edge_encoder is not None:
                self._edge_encoder.append(
                    nn.Linear(edge_in_feats, edge_hidden_feats))

            self._convs.append(GATConv(
                in_hidden,
                edge_hidden_feats,
                out_hidden,
                num_heads,
                norm=norm,
                attn_dropout=attn_dropout,
                edge_dropout=edge_dropout,
                negative_slope=negative_slope,
                residual=residual,
                use_attn_dst=use_attn_dst,
                allow_zero_in_degree=allow_zero_in_degree,
            ))

            if batch_norm:
                self._batch_norms.append(
                    nn.BatchNorm1d(num_heads * out_hidden))

        self._fc_prediction = nn.Linear(
            num_heads * node_hidden_feats, out_feats)

    def forward(
        self,
        g: Union[dgl.DGLGraph, tuple[dgl.DGLGraph]],
    ) -> torch.Tensor:
        if isinstance(g, list):
            x = self._node_encoder(g[0].srcdata['feat'])

            if self._activation is not None:
                x = self._activation(x)
            else:
                x = F.relu(x, inplace=True)

            x = self._input_dropout(x)

            for i in range(self._num_layers):
                if self._edge_encoder is not None:
                    efeat = g[i].edata['feat']

                    efeat_embedding = self._edge_encoder[i](efeat)

                    if self._activation is not None:
                        efeat_embedding = self._activation(efeat_embedding)
                    else:
                        efeat_embedding = F.relu(efeat_embedding, inplace=True)
                else:
                    efeat_embedding = None

                x = self._convs[i](g[i], x, efeat_embedding).flatten(1, -1)

                if self._batch_norms is not None:
                    x = self._batch_norms[i](x)

                if self._activation is not None:
                    x = self._activation(x, inplace=True)

                x = self._dropout(x)
        else:
            x = self._node_encoder(g.srcdata['feat'])
            x = F.relu(x, inplace=True)

            x = self._input_dropout(x)

            for i in range(self._num_layers):
                if self._edge_encoder is not None:
                    efeat = g.edata['feat']

                    efeat_embedding = self._edge_encoder[i](efeat)
                    efeat_embedding = F.relu(efeat_embedding, inplace=True)
                else:
                    efeat_embedding = None

                x = self._convs[i](g, x, efeat_embedding).flatten(1, -1)

                if self._batch_norms is not None:
                    x = self._batch_norms[i](x)

                if self._activation is not None:
                    x = self._activation(x, inplace=True)

                x = self._dropout(x)

        x = self._fc_prediction(x)

        return x


def train_mini_batch(
    model: nn.Module,
    device: Union[str, torch.device],
    optimizer: torch.optim.Optimizer,
    loss_function: Callable[[torch.Tensor, torch.Tensor], torch.Tensor],
    dataloader: dgl.dataloading.NodeDataLoader,
) -> tuple[float]:
    model.train()

    total_loss = 0
    total_accuracy = 0

    start = default_timer()

    for step, (_, _, blocks) in enumerate(dataloader):
        optimizer.zero_grad()

        blocks = [block.int().to(device) for block in blocks]
        labels = blocks[-1].dstdata['label']

        logits = model(blocks)
        loss = loss_function(logits, labels)

        loss.backward()
        optimizer.step()

        _, indices = torch.max(logits, dim=1)
        correct = torch.sum(indices == labels)
        accuracy = correct.item() / len(labels)

        total_loss += loss.item()
        total_accuracy += accuracy

    stop = default_timer()
    time = stop - start

    total_loss /= step + 1
    total_accuracy /= step + 1

    return time, total_loss, total_accuracy


def train_full_graph(
    model: nn.Module,
    optimizer: torch.optim.Optimizer,
    loss_function: Callable[[torch.Tensor, torch.Tensor], torch.Tensor],
    g: dgl.DGLGraph,
    mask: torch.Tensor,
) -> tuple[float]:
    features = g.ndata['feat']
    labels = g.ndata['label']

    model.train()
    optimizer.zero_grad()

    start = default_timer()

    logits = model(g, features)
    loss = loss_function(logits[mask], labels[mask])

    loss.backward()
    optimizer.step()

    _, indices = torch.max(logits[mask], dim=1)
    correct = torch.sum(indices == labels[mask])
    accuracy = correct.item() / len(labels[mask])

    stop = default_timer()
    time = stop - start

    return time, loss, accuracy


def validate(
    model: nn.Module,
    loss_function: Callable[[torch.Tensor, torch.Tensor], torch.Tensor],
    g: dgl.DGLGraph,
    mask: torch.Tensor,
) -> tuple[float]:
    labels = g.ndata['label']

    model.eval()

    start = default_timer()

    with torch.no_grad():
        logits = model(g)
        loss = loss_function(logits[mask], labels[mask])

        _, indices = torch.max(logits[mask], dim=1)
        correct = torch.sum(indices == labels[mask])
        accuracy = correct.item() / len(labels[mask])

    stop = default_timer()
    time = stop - start

    return time, loss, accuracy