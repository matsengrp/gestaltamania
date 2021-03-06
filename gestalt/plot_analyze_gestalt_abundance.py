import sys
import six
import argparse
import os.path
import subprocess

import numpy as np
from scipy import stats
import matplotlib
matplotlib.use('Agg')
from matplotlib import pyplot
import seaborn as sns
from common import parse_comma_str
from plot_analyze_gestalt_meta import load_fish
from sklearn import linear_model

def parse_args(args):
    parser = argparse.ArgumentParser(
            description='plot abundance vs internal node time')
    parser.add_argument(
        '--obs-file-template',
        type=str,
        default="_output/%s/sampling_seed0/fish_data_restrict.pkl")
    parser.add_argument(
        '--mle-file-template',
        type=str,
        default="_output/%s/sampling_seed0/sum_states_20/extra_steps_1/tune_pen_hanging.pkl")
    parser.add_argument(
        '--nj-file-template',
        type=str,
        default="_output/%s/sampling_seed0/nj_fitted.pkl")
    parser.add_argument(
        '--chronos-file-template',
        type=str,
        default="_output/%s/sampling_seed0/chronos_fitted.pkl")
    parser.add_argument(
        '--folder',
        type=str,
        default="analyze_gestalt")
    parser.add_argument(
        '--fishies',
        type=str,
        #default="3day1,3day2,3day3,3day4,3day5")
        default="30hpf_v6_4,30hpf_v6_5,30hpf_v6_6,30hpf_v6_8")
        #default="dome1,dome3,dome5,dome8,dome10")
    parser.add_argument(
        '--out-plot-template',
        type=str,
        default="_output/time_to_abund_%s.png")
    parser.add_argument(
        '--scratch-file',
        type=str,
        default="_output/scratch/lm.txt")
    parser.add_argument(
        '--tot-time',
        type=float,
        default=4.3)
    parser.add_argument(
        '--alpha',
        type=float,
        default=0.5)
    parser.add_argument(
        '--size',
        type=float,
        default=20)
    args = parser.parse_args(args)
    args.fishies = parse_comma_str(args.fishies, str)
    return args

def load_data(args, fish, method):
    if fish in ["ADR1", "ADR2"]:
        return load_fish(fish, method)

    if method == "PMLE":
        fitted_tree_file = os.path.join(args.folder, args.mle_file_template % fish)
        with open(fitted_tree_file, "rb") as f:
            fitted_data = six.moves.cPickle.load(f)
            if "final_fit" in fitted_data:
                fitted_bifurc_tree = fitted_data["final_fit"].fitted_bifurc_tree
            else:
                fitted_bifurc_tree = fitted_data[-1]["best_res"].fitted_bifurc_tree
    elif method == "chronos":
        chronos_tree_file = os.path.join(args.folder, args.chronos_file_template % fish)
        with open(chronos_tree_file, "rb") as f:
            fitted_bifurc_tree = six.moves.cPickle.load(f)[0]["fitted_tree"]
    elif method == "nj":
        nj_tree_file = os.path.join(args.folder, args.nj_file_template % fish)
        with open(nj_tree_file, "rb") as f:
            fitted_bifurc_tree = six.moves.cPickle.load(f)[1]["fitted_tree"]
    else:
        raise ValueError("nope method dont exist")

    obs_file = os.path.join(args.folder, args.obs_file_template % fish)
    with open(obs_file, "rb") as f:
        obs_dict = six.moves.cPickle.load(f)
    return fitted_bifurc_tree, obs_dict

def get_abundance_dists(fitted_bifurc_tree):
    fitted_bifurc_tree.label_dist_to_roots()
    Y_abundance = []
    X_dists = []
    for node in fitted_bifurc_tree.traverse("preorder"):
        if node.dist_to_root < 0.05:
            # Get rid of crazy outliers that might decieve us into thinking our method
            # is wonderful
            continue
        if node.is_leaf():
            continue
        #if min([leaf.abundance for leaf in node]) > 1:
        #    continue
        if len(node.spine_children) == 0:
            continue
        Y_abundance.append(float(sum([leaf.abundance for leaf in node])))
        X_dists.append(node.dist_to_root)
    print("NUM OBS", len(X_dists))
    return X_dists, Y_abundance

def is_linear_test(X_dists, Y_abundance, args):
    X_dist_new = np.array(X_dists).reshape((-1, 1))
    Y_abundance_log = np.log2(Y_abundance).reshape((-1, 1))
    data = np.concatenate([X_dist_new, Y_abundance_log], axis=1)
    np.savetxt(args.scratch_file, data, delimiter=",")
    cmd = [
            'Rscript',
            '../R/fit_linear_regression.R',
            args.scratch_file]
    print(" ".join(cmd))
    res = subprocess.check_output(cmd)
    print(res)
    with open(args.scratch_file, "r") as f:
        print(f.read())

def plot_distance_to_abundance(
        X_dists,
        Y_abundance,
        tot_time,
        args,
        out_plot_file):
    """
    Understand if distance to root is inversely related to total abundance
    """
    if out_plot_file:
        print(out_plot_file)
        pyplot.clf()
        sns.regplot(
                np.log2(Y_abundance), #- np.log2(np.max(Y_abundance)),
                np.array(X_dists) * tot_time,
                #fit_reg=True,
                lowess=True,
                scatter_kws={"alpha": args.alpha, "s": args.size})
        pyplot.ylabel("dist to root")
        pyplot.xlabel("log_2(abundance)")
        pyplot.ylim(-0.1,tot_time + 0.1)
        #pyplot.ylim(
        #        np.min(np.log2(Y_abundance) - np.log2(np.max(Y_abundance))) - 0.15,
        #        0.15)
        pyplot.savefig(out_plot_file)
    slope, _, _, pval, se = stats.linregress(np.log2(Y_abundance), X_dists)
    #print("estimated slope", slope, "pval", pval)
    #print("95 CI", slope - 1.96 * se, slope + 1.96 * se)
    print("estimated time btw divisions %.03f" % (-tot_time * slope * 60))
    print("95 CI (%.03f, %.03f)" % (-tot_time * 60 * (slope - 1.96 * se), -tot_time * 60 * (slope + 1.96 * se)))


def main(args=sys.argv[1:]):
    args = parse_args(args)
    methods = ["PMLE", "chronos", "nj"]
    methods = ["PMLE"]
    for method in methods:
        print("method", method)
        for fish in args.fishies:
            print(fish)
            try:
                fitted_bifurc_tree, obs_data_dict = load_data(args, fish, method)
            except FileNotFoundError:
                continue
            X_dists, Y_abundance = get_abundance_dists(fitted_bifurc_tree)
            is_linear_test(X_dists, Y_abundance, args)
            plot_distance_to_abundance(
                X_dists,
                Y_abundance,
                args.tot_time,
                args,
                out_plot_file = args.out_plot_template % fish)

if __name__ == "__main__":
    main()
