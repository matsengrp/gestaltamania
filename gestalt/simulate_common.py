"""
Share code for simulations
"""
from __future__ import division, print_function
import sys
import numpy as np
import argparse
import matplotlib
matplotlib.use('agg')
from matplotlib import pyplot as plt
import time
import tensorflow as tf
from tensorflow.python import debug as tf_debug
from scipy.stats import pearsonr, spearmanr, kendalltau
import logging
import pickle
from pathlib import Path

from cell_state import CellState, CellTypeTree
from cell_state_simulator import CellTypeSimulator
from clt_simulator import CLTSimulatorBifurcating
from clt_simulator_simple import CLTSimulatorOneLayer, CLTSimulatorTwoLayers
from allele_simulator_simult import AlleleSimulatorSimultaneous
from allele import Allele
from clt_observer import CLTObserver
from clt_estimator import CLTParsimonyEstimator
from clt_likelihood_estimator import *
from alignment import AlignerNW
from barcode_metadata import BarcodeMetadata
from approximator import ApproximatorLB
from tree_manipulation import search_nearby_trees

from constants import *
from common import *
from summary_util import *

def get_parsimony_trees(obs_leaves, args, bcode_meta, true_tree, max_trees):
    parsimony_estimator = CLTParsimonyEstimator(
            bcode_meta,
            args.out_folder,
            args.mix_path)
    #TODO: DOESN'T USE CELL STATE
    parsimony_trees = parsimony_estimator.estimate(
            obs_leaves,
            num_mix_runs=args.num_jumbles)
    logging.info("Total parsimony trees %d", len(parsimony_trees))

    # Sort the parsimony trees into their robinson foulds distance from the truth
    parsimony_tree_dict = {}
    parsimony_score = None
    for tree in parsimony_trees:
        if parsimony_score is None:
            parsimony_score = tree.get_parsimony_score()
        rf_res = true_tree.robinson_foulds(
                tree,
                attr_t1="allele_events_list_str",
                attr_t2="allele_events_list_str",
                expand_polytomies=False,
                unrooted_trees=False)
        rf_dist = rf_res[0]
        rf_dist_max = rf_res[1]
        logging.info(
                "full barcode tree: rf dist %d (max %d) pars %d",
                rf_dist,
                rf_dist_max,
                parsimony_score)
        logging.info(tree.get_ascii(attributes=["allele_events_list_str"], show_internal=True))
        if rf_dist not in parsimony_tree_dict:
            parsimony_tree_dict[rf_dist] = [tree]
        else:
            parsimony_tree_dict[rf_dist].append(tree)

    # make each set of trees for each rf distance uniq
    for k, v in parsimony_tree_dict.items():
        parsimony_tree_dict[k] = CLTParsimonyEstimator.get_uniq_trees(
                v,
                max_trees=args.max_trees)
    return parsimony_tree_dict

def create_cell_type_tree(args):
    # This first rate means nothing!
    cell_type_tree = CellTypeTree(cell_type=0, rate=0.1)
    args.cell_rates = [0.20, 0.25, 0.15, 0.15]
    cell1 = CellTypeTree(cell_type=1, rate=args.cell_rates[0])
    cell2 = CellTypeTree(cell_type=2, rate=args.cell_rates[1])
    cell3 = CellTypeTree(cell_type=3, rate=args.cell_rates[2])
    cell4 = CellTypeTree(cell_type=4, rate=args.cell_rates[3])
    cell_type_tree.add_child(cell1)
    cell_type_tree.add_child(cell2)
    cell2.add_child(cell3)
    cell1.add_child(cell4)
    return cell_type_tree

def create_simulators(args, clt_model):
    allele_simulator = AlleleSimulatorSimultaneous(clt_model)
    # TODO: merge cell type simulator into allele simulator
    cell_type_simulator = CellTypeSimulator(clt_model.cell_type_tree)
    if args.single_layer:
        clt_simulator = CLTSimulatorOneLayer(
                cell_type_simulator,
                allele_simulator)
    elif args.two_layers:
        clt_simulator = CLTSimulatorTwoLayers(
                cell_type_simulator,
                allele_simulator)
    else:
        clt_simulator = CLTSimulatorBifurcating(
                args.birth_lambda,
                args.death_lambda,
                cell_type_simulator,
                allele_simulator)
    observer = CLTObserver()
    return clt_simulator, observer

def create_cell_lineage_tree(args, clt_model):
    clt_simulator, observer = create_simulators(args, clt_model)

    # Keep trying to make CLT until enough leaves in observed tree
    obs_leaves = set()
    MAX_TRIES = 10
    num_tries = 0
    sim_time = args.time
    for i in range(MAX_TRIES):
        clt = clt_simulator.simulate(
            tree_seed = args.model_seed,
            data_seed = args.data_seed,
            time = sim_time,
            max_nodes = args.max_clt_nodes)
        sampling_rate = args.sampling_rate
        while (len(obs_leaves) < args.min_leaves or len(obs_leaves) >= args.max_leaves) and sampling_rate <= 1:
            # Now sample the leaves and create the true topology
            obs_leaves, true_tree = observer.observe_leaves(
                    sampling_rate,
                    clt,
                    seed=args.model_seed,
                    observe_cell_state=args.use_cell_state)

            if true_tree.get_max_depth() > args.max_depth:
                sim_time *= 0.8
                break

            logging.info("sampling rate %f, num leaves %d", sampling_rate, len(obs_leaves))
            num_tries += 1
            if len(obs_leaves) < args.min_leaves:
                sampling_rate += 0.025
            elif len(obs_leaves) >= args.max_leaves:
                sampling_rate = max(1e-3, sampling_rate - 0.05)

    if len(obs_leaves) < args.min_leaves:
        raise Exception("Could not manage to get enough leaves")
    true_tree.label_tree_with_strs()
    logging.info(true_tree.get_ascii(attributes=["allele_events_list_str"], show_internal=True))
    # Check all leaves unique because rf distance code requires leaves to be unique
    # The only reason leaves wouldn't be unique is if we are observing cell state OR
    # we happen to have the same allele arise spontaneously in different parts of the tree.
    if not args.use_cell_state:
        uniq_leaves = set()
        for n in true_tree:
            if n.allele_events_list_str in uniq_leaves:
                logging.info("repeated leaf %s", n.allele_events_list_str)
                clt.label_tree_with_strs()
                logging.info(clt.get_ascii(attributes=["allele_events_list_str"], show_internal=True))
            else:
                uniq_leaves.add(n.allele_events_list_str)
        assert len(set([n.allele_events_list_str for n in true_tree])) == len(true_tree), "leaves must be unique"

    return clt, obs_leaves, true_tree

def compare_lengths(length_dict1, length_dict2, subset, branch_plot_file, label):
    """
    Compares branch lengths, logs the results as well as plots them

    @param subset: the subset of keys in the length dicts to use for the comparison
    @param branch_plot_file: name of file to save the scatter plot to
    @param label: the label for logging/plotting
    """
    length_list1 = []
    length_list2 = []
    for k in subset:
        length_list1.append(length_dict1[k])
        length_list2.append(length_dict2[k])
    logging.info("Compare lengths %s", label)
    logging.info("pearson %s %s", label, pearsonr(length_list1, length_list2))
    logging.info("pearson (log) %s %s", label, pearsonr(np.log(length_list1), np.log(length_list2)))
    logging.info("spearman %s %s", label, spearmanr(length_list1, length_list2))
    logging.info(length_list1)
    logging.info(length_list2)
    plt.scatter(np.log(length_list1), np.log(length_list2))
    plt.savefig(branch_plot_file)

def fit_pen_likelihood(tree, args, bcode_meta, cell_type_tree, approximator, sess):
    num_nodes = len([t for t in tree.traverse()])

    if args.know_target_lambdas:
        target_lams = np.array(args.target_lambdas)
    else:
        target_lams = 0.3 * np.ones(args.target_lambdas.size) + np.random.uniform(size=args.num_targets) * 0.08

    res_model = CLTLikelihoodModel(
        tree,
        bcode_meta,
        sess,
        target_lams = target_lams,
        target_lams_known=args.know_target_lambdas,
        branch_len_inners = np.random.rand(num_nodes) * 0.1,
        cell_type_tree = cell_type_tree if args.use_cell_state else None,
        cell_lambdas_known = args.know_cell_lambdas)
    estimator = CLTPenalizedEstimator(
            res_model,
            approximator,
            args.log_barr)
    pen_log_lik = estimator.fit(
            args.num_inits,
            args.max_iters)
    return pen_log_lik, res_model