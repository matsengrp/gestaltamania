"""
Tunes over different multifurcating topologies.
Basically a wrapper around `run_estimator.py`
This will create jobs to run on the cluster for each of the trees
"""
import sys
import six
import os
import glob
import argparse
import logging
import six
import numpy as np
import random
from typing import List, Tuple, Dict

from parallel_worker import BatchSubmissionManager
from estimator_worker import RunEstimatorWorker
from likelihood_scorer import LikelihoodScorerResult
from common import create_directory

def parse_args():
    parser = argparse.ArgumentParser(description='tune over many multifurcating topologies and fit model parameters')
    parser.add_argument(
        '--obs-file',
        type=str,
        default="_output/obs_data.pkl",
        help='pkl file with observed sequence data, should be a dict with ObservedAlignSeq')
    parser.add_argument(
        '--topology-file-template',
        type=str,
        default="_output/parsimony_tree0.pkl",
        help="""
        We look in the directory of this path for any trees that need to be processed.
        We replace the 0 with a *[0-9] when we grep for matching trees.
        """)
    parser.add_argument(
        '--out-model-file',
        type=str,
        default="_output/tune_topology_fitted.pkl")
    parser.add_argument(
        '--log-file',
        type=str,
        default="_output/log_tune_topology.txt")
    parser.add_argument(
        '--true-model-file',
        type=str,
        default=None,
        help='pkl file with true model if available')
    parser.add_argument(
        '--seed',
        type=int,
        default=40)
    parser.add_argument(
        '--log-barr',
        type=float,
        default=0.001,
        help="log barrier parameter on the branch lengths")
    parser.add_argument(
        '--dist-to-half-pens',
        type=str,
        default='1',
        help="comma-separated string with penalty parameters on the target lambdas")
    parser.add_argument(
        '--total-tune-splits',
        type=int,
        default=1,
        help="""
        number of random splits of the data for tuning penalty params
        across all topologies we tune
        """)
    parser.add_argument('--max-iters', type=int, default=20)
    parser.add_argument('--num-inits', type=int, default=1)
    parser.add_argument(
        '--max-sum-states',
        type=int,
        default=None,
        help='maximum number of internal states to marginalize over')
    parser.add_argument(
        '--max-extra-steps',
        type=int,
        default=1,
        help='maximum number of extra steps to explore possible ancestral states')
    parser.add_argument(
        '--cpu-threads',
        type=int,
        default=6,
        help='number of cpu threads to request in srun when submitting jobs')
    parser.add_argument(
        '--submit-srun',
        action='store_true',
        help='is using slurm to submit jobs')
    parser.add_argument(
        '--lambda-known',
        action='store_true',
        help='are target rates known?')
    parser.add_argument(
        '--tot-time-known',
        action='store_true',
        help='is total time known?')
    parser.add_argument(
        '--max-topologies',
        type=int,
        default=10,
        help='max topologies to tune over')
    parser.add_argument(
        '--fit-all-topologies',
        action='store_true',
        help='whether to fit all topologies anyways')

    parser.set_defaults(fit_all_topologies=False, submit_srun=False)
    args = parser.parse_args()

    assert args.log_barr >= 0
    args.dist_to_half_pen_list = list(sorted(
        [float(lam) for lam in args.dist_to_half_pens.split(",")],
        reverse=True))

    create_directory(args.out_model_file)
    args.topology_folder = os.path.dirname(args.topology_file_template)
    args.scratch_dir = os.path.join(
            args.topology_folder,
            'scratch')
    if not os.path.exists(args.scratch_dir):
        os.mkdir(args.scratch_dir)

    return args

def get_best_hyperparam(
        args,
        tune_results: List[Tuple[Dict, RunEstimatorWorker]]):
    """
    Grabs out the best hyperparam given the results
    Figures out best penalty param first by aggregating across different topologies.
    Then for that chosen penalty param, find the highest scoring topology.

    @return (
        list of LikelihoodScorerResult,
        best dist-to-half penalty,
        best topology idx in the list of TuneScorerResult)
    """
    # First pick out the best penalty param
    # TODO: aggregating across different topologies
    # for a bit of stability... i think this helps?
    total_pen_param_score = np.zeros(len(args.dist_to_half_pen_list))
    for (topology_res, _) in tune_results:
        for i, res in enumerate(topology_res["tune_results"]):
            total_pen_param_score[i] += res.score

    logging.info("Total tuning scores %s", total_pen_param_score)
    best_idx = np.argmax(total_pen_param_score)
    best_dist_to_half_pen = args.dist_to_half_pen_list[best_idx]
    logging.info("Best penalty param %s", best_dist_to_half_pen)

    # Now identify the best topology
    topology_scores = [
        topology_res["tune_results"][best_idx].score
        for (topology_res, _) in tune_results]
    best_topology_idx = np.argmax(topology_scores)
    logging.info("Worker scores %s", topology_scores)
    logging.info("Best topology %d, %s",
            best_topology_idx,
            tune_results[best_topology_idx][1].topology_file)

    if not args.fit_all_topologies:
        return [tune_results[best_topology_idx]], best_idx, 0
    else:
        return tune_results, best_idx, best_topology_idx

def tune_hyperparams(
        topology_files: List[str],
        args):
    """
    Tunes both the penalty parameter as well as the topology
    Uses the "score" of the topology penalty parameter pair to pick out which
    final model to fit. (score here is the stability score)

    @return (
        list of LikelihoodScorerResult,
        best dist-to-half penalty,
        best topology idx in the list of TuneScorerResult)
    """
    worker_list = []
    for file_idx, top_file in enumerate(topology_files):
        worker = RunEstimatorWorker(
            args.obs_file,
            top_file,
            args.out_model_file.replace(".pkl", "_tune_only_tree%d.pkl" % file_idx),
            None,
            args.true_model_file,
            args.seed + file_idx,
            args.log_barr,
            args.dist_to_half_pens,
            args.max_iters,
            # When tuning hyper-params, we just use one initialization
            num_inits = 1,
            lambda_known = args.lambda_known,
            tot_time_known = args.tot_time_known,
            do_refit = False,
            tune_only = True,
            max_sum_states = args.max_sum_states,
            max_extra_steps = args.max_extra_steps,
            num_tune_splits = args.total_tune_splits,
            scratch_dir = args.scratch_dir)
        worker_list.append(worker)

    # Just print the things I plan to run
    for w in worker_list:
        w.run_worker(None, debug=True)

    if args.submit_srun:
        logging.info("Submitting jobs")
        job_manager = BatchSubmissionManager(
                worker_list,
                None,
                len(worker_list),
                args.scratch_dir,
                threads=args.cpu_threads)
        successful_workers = job_manager.run(successful_only=True)
        assert len(successful_workers) > 0
    else:
        logging.info("Running locally")
        successful_workers = [(w.run_worker(None), w) for w in worker_list]

    return get_best_hyperparam(
            args,
            successful_workers)

def create_topology_warm_start_files(
        tune_results: List[LikelihoodScorerResult],
        best_pen_param_idx: int,
        args):
    """
    Creates warm start files for refitting topologies

    @return List[topology file name, warm start file name] for each element in `tune_results`
    """
    topology_warm_start_files = []
    for idx, (topology_res, worker) in enumerate(tune_results):
        # Pick an arbitrary model param setting for warm start
        warm_start = topology_res["tune_results"][best_pen_param_idx].model_params_dicts[0]
        warm_start_file_name = args.out_model_file.replace(
                ".pkl",
                "_warm%d_all%d.pkl" % (idx, args.fit_all_topologies))
        with open(warm_start_file_name, "wb") as f:
            six.moves.cPickle.dump(warm_start, f, protocol=2)
        topology_warm_start_files.append((
            worker.topology_file,
            warm_start_file_name))
    return topology_warm_start_files

def fit_models(
        topology_warm_start_files: List[Tuple[str, str]],
        args,
        pen_param: float):
    """
    Fits the models for the list of files in `topology_warm_start_files`

    @param topology_warm_start_files: list of topology file and warm start file pairs
    @param pen_param: the dist-to-half penalty parameter to fit

    @return a list of results in the form of List[(LikelihoodScorerResult, EstimatorWorker)]
    """
    worker_list = []
    for file_idx, (top_file, warm_start_file) in enumerate(topology_warm_start_files):
        worker = RunEstimatorWorker(
            args.obs_file,
            top_file,
            args.out_model_file.replace(".pkl", "_refit%d_all%s.pkl" % (file_idx, args.fit_all_topologies)),
            warm_start_file,
            args.true_model_file,
            args.seed + file_idx,
            args.log_barr,
            str(pen_param),
            args.max_iters,
            num_inits = args.num_inits,
            lambda_known = args.lambda_known,
            tot_time_known = args.tot_time_known,
            do_refit = True,
            tune_only = False,
            max_sum_states = args.max_sum_states,
            max_extra_steps = args.max_extra_steps,
            num_tune_splits = 0, # No more tuning
            scratch_dir = args.scratch_dir)
        worker_list.append(worker)

    for w in worker_list:
        w.run_worker(None, debug=True)

    if args.submit_srun:
        logging.info("Submitting jobs")
        job_manager = BatchSubmissionManager(
                worker_list,
                None,
                len(worker_list),
                args.scratch_dir,
                threads=args.cpu_threads)
        successful_workers = job_manager.run()
    else:
        logging.info("Running locally")
        successful_workers = [(w.run_worker(None), w) for w in worker_list]

    return successful_workers

def main(args=sys.argv[1:]):
    args = parse_args()
    logging.basicConfig(format="%(message)s", filename=args.log_file, level=logging.DEBUG)
    logging.info(str(args))

    with open(args.obs_file, "rb") as f:
        obs_data_dict = six.moves.cPickle.load(f)
        bcode_meta = obs_data_dict["bcode_meta"]
        args.num_barcodes = bcode_meta.num_barcodes

    np.random.seed(args.seed)
    random.seed(args.seed)

    # Find all the relevant topology files from max parsimony
    all_topology_files = glob.glob(args.topology_file_template.replace("parsimony_tree0", "parsimony_tree*[0-9]"))
    random.shuffle(all_topology_files)
    topology_files = all_topology_files[:args.max_topologies]
    assert len(topology_files) > 0
    logging.info("Processing the tree files: %s", topology_files)

    # Actually tune things -- tune penalty params and tune topology
    tune_results, best_pen_param_idx, best_topology_idx = tune_hyperparams(topology_files, args)
    topology_warm_start_files = create_topology_warm_start_files(
            tune_results,
            best_pen_param_idx,
            args)
    results = fit_models(
            topology_warm_start_files,
            args,
            args.dist_to_half_pen_list[best_pen_param_idx])

    # Copy out the results from the worker that we chose according to our scoring criteria
    best_worker = results[best_topology_idx][1]
    with open(best_worker.out_model_file, "rb") as f:
        results = six.moves.cPickle.load(f)
    with open(args.out_model_file, "wb") as f:
        six.moves.cPickle.dump(results, f, protocol = 2)
    logging.info("Complete!!!")

if __name__ == "__main__":
    main()
