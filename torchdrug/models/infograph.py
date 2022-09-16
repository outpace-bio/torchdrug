import copy
import random

import torch
from torch import nn
from torch.nn import functional as F

from torchdrug import core, layers
from torchdrug.core import Registry as R


@R.register("models.InfoGraph")
class InfoGraph(nn.Module, core.Configurable):
    """
    InfoGraph proposed in
    `InfoGraph: Unsupervised and Semi-supervised Graph-Level Representation Learning via Mutual Information
    Maximization`_.

    .. _InfoGraph\:
        Unsupervised and Semi-supervised Graph-Level Representation Learning via Mutual Information Maximization:
        https://arxiv.org/pdf/1908.01000.pdf

    Parameters:
        model (nn.Module): node & graph representation model
        num_mlp_layer (int, optional): number of MLP layers in mutual information estimators
        activation (str or function, optional): activation function
        loss_weight (float, optional): weight of both unsupervised & transfer losses
        separate_model (bool, optional): separate supervised and unsupervised encoders.
            If true, the unsupervised loss will be applied on a separate encoder,
            and a transfer loss is applied between the two encoders.
    """

    def __init__(self, model, num_mlp_layer=2, activation="relu", loss_weight=1, separate_model=False):
        super(InfoGraph, self).__init__()
        self.model = model
        self.separate_model = separate_model
        self.loss_weight = loss_weight
        self.output_dim = self.model.output_dim

        if separate_model:
            self.unsupervised_model = copy.deepcopy(model)
            self.transfer_mi = layers.MutualInformation(model.output_dim, num_mlp_layer, activation)
        else:
            self.unsupervised_model = model
        self.unsupervised_mi = layers.MutualInformation(model.output_dim, num_mlp_layer, activation)

    def forward(self, graph, input, all_loss=None, metric=None):
        """
        Compute the node representations and the graph representation(s).
        Add the mutual information between graph and nodes to the loss.

        Parameters:
            graph (Graph): :math:`n` graph(s)
            input (Tensor): input node representations
            all_loss (Tensor, optional): if specified, add loss to this tensor
            metric (dict, optional): if specified, output metrics to this dict

        Returns:
            dict with ``node_feature`` and ``graph_feature`` fields:
                node representations of shape :math:`(|V|, d)`, graph representations of shape :math:`(n, d)`
        """
        output = self.model(graph, input)

        if all_loss is not None:
            if self.separate_model:
                unsupervised_output = self.unsupervised_model(graph, input)
                mutual_info = self.transfer_mi(output["graph_feature"], unsupervised_output["graph_feature"])

                metric["distillation mutual information"] = mutual_info
                if self.loss_weight > 0:
                    all_loss -= mutual_info * self.loss_weight
            else:
                unsupervised_output = output

            graph_index = graph.node2graph
            node_index = torch.arange(graph.num_node, device=graph.device)
            pair_index = torch.stack([graph_index, node_index], dim=-1)

            mutual_info = self.unsupervised_mi(unsupervised_output["graph_feature"],
                                               unsupervised_output["node_feature"], pair_index)

            metric["graph-node mutual information"] = mutual_info
            if self.loss_weight > 0:
                all_loss -= mutual_info * self.loss_weight

        return output


@R.register("models.MultiviewContrast")
class MultiviewContrast(nn.Module, core.Configurable):
    """
    Multiview Contrast proposed in `Protein Representation Learning by Geometric Structure Pretraining`_.

    .. _Protein Representation Learning by Geometric Structure Pretraining:
        https://arxiv.org/pdf/2203.06125.pdf

    Parameters:
        model (nn.Module): node & graph representation model
        crop_funcs (list of nn.Module): list of cropping functions
        noise_funcs (list of nn.Module): list of noise functions
        num_mlp_layer (int, optional): number of MLP layers in mutual information estimators
        activation (str or function, optional): activation function
        tau (float, optional): temperature in InfoNCE loss
    """

    eps = 1e-10

    def __init__(self, model, crop_funcs, noise_funcs, num_mlp_layer=2, activation="relu", tau=0.07):
        super(MultiviewContrast, self).__init__()
        self.model = model
        self.crop_funcs = crop_funcs
        self.noise_funcs = noise_funcs
        self.tau = tau

        self.mlp = layers.MLP(model.output_dim, [model.output_dim] * num_mlp_layer, activation=activation)

    def forward(self, graph, input, all_loss=None, metric=None):
        """
        Compute the graph representations of two augmented views.
        Each view is generated by randomly picking a cropping function and a noise function.
        Add the mutual information between two augmented views to the loss.

        Parameters:
            graph (Graph): :math:`n` graph(s)
            input (Tensor): input node representations
            all_loss (Tensor, optional): if specified, add loss to this tensor
            metric (dict, optional): if specified, output metrics to this dict

        Returns:
            dict with ``node_feature1``, ``node_feature2``, ``graph_feature1`` and ``graph_feature2`` fields:
                node representations of shape :math:`(|V|, d)`, graph representations of shape :math:`(n, d)`
                for two augmented views respectively
        """
        # Get two augmented views
        graph = copy.copy(graph)
        with graph.residue():
            graph.input = input
        crop_func1, noise_func1 = random.sample(self.crop_funcs, 1)[0], random.sample(self.noise_funcs, 1)[0]
        graph1 = crop_func1(graph)
        graph1 = noise_func1(graph1)
        output1 = self.model(graph1, graph1.input)

        crop_func2, noise_func2 = random.sample(self.crop_funcs, 1)[0], random.sample(self.noise_funcs, 1)[0]
        graph2 = crop_func2(graph)
        graph2 = noise_func2(graph2)
        output2 = self.model(graph2, graph2.input)

        # Compute mutual information loss
        if all_loss is not None:
            x = self.mlp(output1["graph_feature"])
            y = self.mlp(output2["graph_feature"])

            score = F.cosine_similarity(x.unsqueeze(1), y.unsqueeze(0), dim=-1)
            score = score / self.tau
            is_positive = torch.diag(torch.ones(len(x), dtype=torch.bool, device=self.device))
            mutual_info = (score[is_positive] - score.logsumexp(dim=-1)).mean()

            metric["multiview mutual information"] = mutual_info
            all_loss -= mutual_info

        output = {"node_feature1": output1["node_feature"], "graph_feature1": output1["graph_feature"],
                  "node_feature2": output2["node_feature"], "graph_feature2": output2["graph_feature"]}
        return output
