import time
import numpy as np
import tensorflow as tf
from tensorflow import Tensor

from typing import List, Dict
from numpy import ndarray
from tensorflow import Session

from cell_lineage_tree import CellLineageTree
from cell_state import CellTypeTree
from barcode_metadata import BarcodeMetadata
from indel_sets import TargetTract, SingletonWC, Singleton
from target_status import TargetStatus, TargetDeactTract
from anc_state import AncState
from transition_wrapper_maker import TransitionWrapper
import tf_common
from common import inv_sigmoid
from constants import PERTURB_ZERO


class CLTLikelihoodModel:
    """
    Stores model parameters and branch lengths
    """
    NODE_ORDER = "preorder"

    def __init__(self,
            topology: CellLineageTree,
            bcode_meta: BarcodeMetadata,
            sess: Session,
            target_lams: ndarray,
            target_lams_known: bool = False,
            trim_long_probs: ndarray = 0.05 * np.ones(2),
            trim_zero_prob: float = 0.5,
            trim_poissons: ndarray = 2.5 * np.ones(2),
            insert_zero_prob: float = 0.5,
            insert_poisson: float = 0.2,
            double_cut_weight: float = 1.0,
            branch_len_inners: ndarray = np.array([]),
            branch_len_offsets: ndarray = np.array([]),
            cell_type_tree: CellTypeTree = None,
            cell_lambdas_known: bool = False,
            tot_time: float = 1):
        """
        @param topology: provides a topology only (ignore any branch lengths in this tree)
        TODO: this is currently known to the model, even though it shouldn't be! we should make this a variable too
        @param double_cut_weight: a weight for inter-target indels
        @param target_lams: target lambda rates
        @param target_lams_known: if True, do not make target_lams a parameter that can be tuned. so target_lams would be fixed
        @param cell_type_tree: CellTypeTree that specifies the cell type lambdas
        @param cell_lambdas_known: if True, do not make cell lambdas a parameter that can be tuned.
        @param tot_time: total height of the tree
        """
        self.topology = topology
        self.num_nodes = 0
        if self.topology:
            self.root_node_id = self.topology.node_id
            assert self.root_node_id == 0
            self.num_nodes = self.topology.get_num_nodes()
        self.bcode_meta = bcode_meta
        self.targ_stat_transitions_dict = TargetStatus.get_all_transitions(self.bcode_meta)

        self.num_targets = bcode_meta.n_targets
        self.double_cut_weight = double_cut_weight

        # Process cell type tree
        self.cell_type_tree = cell_type_tree
        cell_type_lams = []
        if cell_type_tree:
            max_cell_type = 0
            cell_type_dict = {}
            for node in cell_type_tree.traverse(self.NODE_ORDER):
                if node.is_root():
                    self.cell_type_root = node.cell_type
                cell_type_dict[node.cell_type] = node.rate
                max_cell_type = max(max_cell_type, node.cell_type)
            for i in range(max_cell_type + 1):
                cell_type_lams.append(cell_type_dict[i])
        cell_type_lams = np.array(cell_type_lams)

        # Save tensorflow session
        self.sess = sess

        self.target_lams_known = target_lams_known
        self.cell_lambdas_known = cell_lambdas_known

        # Stores the penalty parameter
        self.log_barr_ph = tf.placeholder(tf.float64)
        self.tot_time_ph = tf.placeholder(tf.float64)
        self.tot_time = tot_time

        if branch_len_inners.size == 0:
            branch_len_inners = np.ones(self.num_nodes)
        if branch_len_offsets.size == 0:
            branch_len_offsets = np.ones(self.num_nodes)

        # Create all the variables
        self._create_parameters(
                target_lams,
                trim_long_probs,
                [trim_zero_prob],
                trim_poissons,
                [insert_zero_prob],
                [insert_poisson],
                branch_len_inners,
                branch_len_offsets,
                cell_type_lams)
        if self.topology:
            self.branch_lens = self._create_branch_lens()

        # Calculcate the hazards for all the target tracts beforehand. Speeds up computation in the future.
        self.target_tract_hazards, self.target_tract_dict = self._create_all_target_tract_hazards()
        # Dictionary for storing hazards between target statuses -- assuming all moves are possible
        self.targ_stat_transition_hazards_dict = {
            start_target_status: {} for start_target_status in self.targ_stat_transitions_dict.keys()}
        # Calculate hazard for transitioning away from all target statuses beforehand. Speeds up future computation.
        self.hazard_away_dict = self._create_hazard_away_dict()

        self.adam_opt = tf.train.AdamOptimizer(learning_rate=0.005)

    def _create_parameters(self,
            target_lams: ndarray,
            trim_long_probs: ndarray,
            trim_zero_prob: float,
            trim_poissons: ndarray,
            insert_zero_prob: float,
            insert_poisson: float,
            branch_len_inners: ndarray,
            branch_len_offsets: ndarray,
            cell_type_lams: ndarray):
        # Fix the first target value -- not for optimization
        model_params = np.concatenate([
                    [] if self.target_lams_known else np.log(target_lams[1:]),
                    inv_sigmoid(trim_long_probs),
                    inv_sigmoid(trim_zero_prob),
                    np.log(trim_poissons),
                    inv_sigmoid(insert_zero_prob),
                    np.log(insert_poisson),
                    np.log(branch_len_inners),
                    np.log(branch_len_offsets),
                    [] if self.cell_lambdas_known else np.log(cell_type_lams)])
        self.all_vars = tf.Variable(model_params, dtype=tf.float64)
        self.all_vars_ph = tf.placeholder(tf.float64, shape=self.all_vars.shape)
        self.assign_op = self.all_vars.assign(self.all_vars_ph)

        # For easy access to these model parameters
        # First target lambda is fixed. The rest can vary. Addresses scaling issues.
        if self.target_lams_known:
            up_to_size = 0
            self.target_lams = tf.constant(target_lams, dtype=tf.float64)
        else:
            up_to_size = target_lams.size - 1
            self.target_lams = tf.concat([
                    tf.constant([target_lams[0]], dtype=tf.float64),
                    tf.exp(self.all_vars[:up_to_size])],
                    axis=0)
        prev_size = up_to_size
        up_to_size += trim_long_probs.size
        self.trim_long_probs = tf.sigmoid(self.all_vars[prev_size: up_to_size])
        self.trim_short_probs = tf.ones(1, dtype=tf.float64) - self.trim_long_probs
        prev_size = up_to_size
        up_to_size += 1
        self.trim_zero_prob = tf.sigmoid(self.all_vars[prev_size: up_to_size])
        prev_size = up_to_size
        up_to_size += trim_poissons.size
        self.trim_poissons = tf.exp(self.all_vars[prev_size: up_to_size])
        prev_size = up_to_size
        up_to_size += 1
        self.insert_zero_prob = tf.sigmoid(self.all_vars[prev_size: up_to_size])
        prev_size = up_to_size
        up_to_size += 1
        self.insert_poisson = tf.exp(self.all_vars[prev_size: up_to_size])
        prev_size = up_to_size
        up_to_size += branch_len_inners.size
        self.branch_len_inners = tf.exp(self.all_vars[prev_size: up_to_size])
        prev_size = up_to_size
        up_to_size += branch_len_offsets.size
        self.branch_len_offsets = tf.exp(self.all_vars[prev_size: up_to_size])
        if self.cell_type_tree:
            if self.cell_lambdas_known:
                self.cell_type_lams = tf.constant(cell_type_lams, dtype=tf.float64)
            else:
                self.cell_type_lams = tf.exp(self.all_vars[-cell_type_lams.size:])
        else:
            self.cell_type_lams = tf.zeros([])

        # Create my poisson distributions
        self.poiss_left = tf.contrib.distributions.Poisson(self.trim_poissons[0])
        self.poiss_right = tf.contrib.distributions.Poisson(self.trim_poissons[1])
        self.poiss_insert = tf.contrib.distributions.Poisson(self.insert_poisson)

    def _create_branch_lens(self):
        """
        Create the branch length variable
        """
        branch_lens_dict = []
        dist_to_root = {self.root_node_id: 0}
        for node in self.topology.traverse("preorder"):
            if node.is_root():
                continue

            if not node.up.is_resolved_multifurcation():
                if node.is_leaf():
                    # A leaf node, use its offset to determine branch length
                    branch_lens_dict.append([
                        [node.node_id],
                        self.tot_time_ph - dist_to_root[node.up.node_id] - self.branch_len_offsets[node.node_id]])
                else:
                    # Internal node -- use the branch length minus offset to specify the branch length (in the bifurcating tree)
                    # But use the offset + branch length to determine distance from root
                    branch_lens_dict.append([
                        [node.node_id],
                        self.branch_len_inners[node.node_id] - self.branch_len_offsets[node.node_id]])
                    dist_to_root[node.node_id] = dist_to_root[node.up.node_id] + self.branch_len_inners[node.node_id]
            else:
                if not node.is_leaf():
                    branch_lens_dict.append([
                        [node.node_id],
                        self.branch_len_inners[node.node_id]])
                    dist_to_root[node.node_id] = dist_to_root[node.up.node_id] + self.branch_len_inners[node.node_id]
                else:
                    branch_lens_dict.append([
                        [node.node_id],
                        self.tot_time_ph - dist_to_root[node.up.node_id]])

        return tf_common.scatter_nd(
                branch_lens_dict,
                output_shape=self.branch_len_inners.shape,
                name="branch_lengths")

    def set_params_from_dict(self, param_dict: Dict[str, ndarray]):
        self.set_params(
                param_dict["target_lams"],
                param_dict["trim_long_probs"],
                param_dict["trim_zero_prob"],
                param_dict["trim_poissons"],
                param_dict["insert_zero_prob"],
                param_dict["insert_poisson"],
                param_dict["branch_len_inners"],
                param_dict["branch_len_offsets"],
                param_dict["cell_type_lams"],
                param_dict["tot_time"])

    def set_params(self,
            target_lams: ndarray,
            trim_long_probs: ndarray,
            trim_zero_prob: float,
            trim_poissons: ndarray,
            insert_zero_prob: float,
            insert_poisson: float,
            branch_len_inners: ndarray,
            branch_len_offsets: ndarray,
            cell_type_lams: ndarray,
            tot_time: float):
        """
        Set model params
        Should be very similar code to _create_parameters
        """
        self.tot_time = tot_time
        if self.cell_type_tree is None:
            cell_type_lams = np.array([])

        init_val = np.concatenate([
            [] if self.target_lams_known else np.log(target_lams[1:]),
            inv_sigmoid(trim_long_probs),
            inv_sigmoid(trim_zero_prob),
            np.log(trim_poissons),
            inv_sigmoid(insert_zero_prob),
            np.log(insert_poisson),
            np.log(branch_len_inners),
            np.log(branch_len_offsets),
            [] if self.cell_lambdas_known else np.log(cell_type_lams)])
        self.sess.run(self.assign_op, feed_dict={self.all_vars_ph: init_val})

    def get_vars(self):
        """
        @return the variable values -- companion for set_params (aka the ordering of the output matches set_params)
        """
        return self.sess.run([
            self.target_lams,
            self.trim_long_probs,
            self.trim_zero_prob,
            self.trim_poissons,
            self.insert_zero_prob,
            self.insert_poisson,
            self.branch_len_inners,
            self.branch_len_offsets,
            self.cell_type_lams]) + [self.tot_time]

    def get_vars_as_dict(self):
        """
        @return the variable values as dictionary instead
        """
        var_vals = self.get_vars()
        var_labels = [
            "target_lams",
            "trim_long_probs",
            "trim_zero_prob",
            "trim_poissons",
            "insert_zero_prob",
            "insert_poisson",
            "branch_len_inners",
            "branch_len_offsets",
            "cell_type_lams"]
        var_dict = {lab: val for lab, val in zip(var_labels, var_vals)}
        var_dict["tot_time"] = self.tot_time
        return var_dict

    def get_branch_lens(self):
        """
        @return dictionary of branch length (node id to branch length)
        """
        return self.sess.run(self.branch_lens, feed_dict={
            self.tot_time_ph: self.tot_time})

    def initialize_branch_lens(self,
            max_attempts: int=10,
            br_len_scale: float=0.5,
            br_len_shrink: float=0.8):
        """
        Will randomly initialize branch lengths if they are not all positive already
        @param max_attempts: will try at most this many times to initialize branch lengths
        """
        def _are_all_branch_lens_positive():
            return all([b > 0 for b in self.get_branch_lens()[1:]])

        for j in range(max_attempts):
            print("attempt %d to make br lens all positive" % j)
            # Keep initializing branch lengths until they are all positive
            model_vars = self.get_vars_as_dict()
            model_vars["branch_len_inners"] = np.random.rand(self.num_nodes) * br_len_scale

            # Initialize branch length offsets
            model_vars["branch_len_offsets"] = model_vars["branch_len_inners"] * 0.25
            for node in self.topology.traverse():
                if node.is_root() or node.up.is_resolved_multifurcation():
                    # Make sure the root or bifurcating nodes don't have offsets
                    model_vars["branch_len_offsets"][node.node_id] = 0

            # If all branch length positive, then we are good to go
            if _are_all_branch_lens_positive():
                break
            br_len_scale *= br_len_shrink

            self.set_params_from_dict(model_vars)

        assert _are_all_branch_lens_positive()
        # This line is just to check that the tree is initialized to be ultrametric
        self.get_fitted_bifurcating_tree()

    def get_all_target_tract_hazards(self):
        return self.sess.run(self.target_tract_hazards)

    def _create_all_target_tract_hazards(self):
        """
        @return Tuple with:
            tensorflow tensor with hazards for all possible target tracts
            dictionary mapping all possible target tracts to their index in the tensor
        """
        target_status_all_active = TargetStatus()
        all_target_tracts = target_status_all_active.get_possible_target_tracts(self.bcode_meta)
        tt_dict = {tt: i for i, tt in enumerate(all_target_tracts)}

        min_targets = tf.constant([tt.min_target for tt in all_target_tracts], dtype=tf.int32)
        max_targets = tf.constant([tt.max_target for tt in all_target_tracts], dtype=tf.int32)
        long_left_statuses = tf.constant([tt.is_left_long for tt in all_target_tracts], dtype=tf.float64)
        long_right_statuses = tf.constant([tt.is_right_long for tt in all_target_tracts], dtype=tf.float64)

        all_hazards = self._create_hazard_target_tract(min_targets, max_targets, long_left_statuses, long_right_statuses)
        return all_hazards, tt_dict

    def _create_hazard_target_tract(self,
            min_target: Tensor,
            max_target: Tensor,
            long_left_statuses: Tensor,
            long_right_statuses: Tensor):
        """
        Creates tensorflow node for calculating hazard of a target tract

        @param min_target: the minimum target that was cut
        @param max_target: the maximum target that was cut
        @param long_left_statuses: 1 if long left trim, 0 if short left trim
        @param long_right_statuses: 1 if long right trim, 0 if short left trim

        The arguments should all have the same length.
        The i-th elem in each argument corresponds to the target tract that was introduced.

        @return tensorflow tensor with the i-th value corresponding to the i-th target tract in the arguments
        """
        # Compute the hazard
        # Adding a weight for double cuts for now
        log_lambda_part = tf.log(tf.gather(self.target_lams, min_target)) + tf.log(tf.gather(self.target_lams, max_target) * self.double_cut_weight) * tf_common.not_equal_float(min_target, max_target)
        left_trim_prob = tf_common.ifelse(long_left_statuses, self.trim_long_probs[0], 1 - self.trim_long_probs[0])
        right_trim_prob = tf_common.ifelse(long_right_statuses, self.trim_long_probs[1], 1 - self.trim_long_probs[1])
        hazard = tf.exp(log_lambda_part + tf.log(left_trim_prob) + tf.log(right_trim_prob), name="hazard")
        return hazard

    def _create_hazard_away_dict(self):
        """
        @return Dictionary mapping all possible TargetStatus to tensorflow tensor for the hazard away
        """
        target_statuses = list(self.targ_stat_transitions_dict.keys())

        # Gets hazards (by tensorflow)
        hazard_away_nodes = self._create_hazard_away_target_statuses(target_statuses)
        hazard_away_dict = {
                targ_stat: hazard_away_nodes[i]
                for i, targ_stat in enumerate(target_statuses)}
        return hazard_away_dict

    def _create_hazard_away_target_statuses(self, target_statuses: List[TargetStatus]):
        """
        @param target_statuses: list of target statuses that we want to calculate the hazard of transitioning away from

        @return tensorflow tensor with the hazards for transitioning away from each of the target statuses
        """
        left_trimmables = []
        right_trimmables = []
        for targ_stat in target_statuses:
            left_m, right_m = self._get_hazard_masks_target_status(targ_stat)
            left_trimmables.append(left_m)
            right_trimmables.append(right_m)

        # Compute the hazard away
        focal_hazards = self._create_hazard_list(True, True, left_trimmables, right_trimmables)
        left_hazards = self._create_hazard_list(True, False, left_trimmables, right_trimmables)[:, :self.num_targets - 1]
        right_hazards = self._create_hazard_list(False, True, left_trimmables, right_trimmables)[:, 1:]
        left_cum_hazards = tf.cumsum(left_hazards, axis=1)
        inter_target_hazards = tf.multiply(left_cum_hazards, right_hazards)
        hazard_away_nodes = tf.add(
                tf.reduce_sum(focal_hazards, axis=1),
                tf.reduce_sum(inter_target_hazards, axis=1),
                name="hazard_away")
        return hazard_away_nodes

    def _get_hazard_masks_target_status(self, target_status: TargetStatus):
        """
        @return two lists, one for left trims and one for right trims.
                each list is composed of values [0,1,2] matching each target.
                0 = no trim at that target (in that direction) can be performed
                1 = only short trim at that target (in that dir) is allowed
                2 = short and long trims are allowed at that target

                We assume no long left trims are allowed at target 0 and any target T
                where T - 1 is deactivated. No long right trims are allowed at the last
                target and any target T where T + 1 is deactivated.
        """
        left_trim_allows = np.ones(self.num_targets, dtype=int) * 2
        left_trim_allows[0] = 1
        for deact_tract in target_status:
            left_trim_allows[deact_tract.min_deact_target:deact_tract.max_deact_target + 1] = 0

        right_trim_allows = np.ones(self.num_targets, dtype=int) * 2
        right_trim_allows[-1] = 1
        for deact_tract in target_status:
            right_trim_allows[deact_tract.min_deact_target:deact_tract.max_deact_target + 1] = 0
        return left_trim_allows.tolist(), right_trim_allows.tolist()

    def _create_hazard_list(self,
            trim_left: bool,
            trim_right: bool,
            left_trimmables: List[List[int]],
            right_trimmables: List[List[int]]):
        """
        Helper function for creating hazard list nodes -- useful for calculating hazard away

        @param trim_left: boolean to output the hazard involving left trims
        @param trim_right: boolean to output the hazard involving right trims
        @param left_trimmables: the mask output from `_get_hazard_masks_target_status` for the left trims
                                indicates whether we can do no trim, short trims only, or long and short trims
        @param right_trimmables: the mask output from `_get_hazard_masks_target_status` for the right trims
                                indicates whether we can do no trim, short trims only, or long and short trims

        @return tensorflow tensor with the hazard of a long and/or short trim to the left and/or right at each of the targets
        """
        trim_short_left = 1 - self.trim_long_probs[0] if trim_left else 1
        trim_short_right = 1 - self.trim_long_probs[1] if trim_right else 1

        left_factor = tf_common.equal_float(left_trimmables, 1) * trim_short_left + tf_common.equal_float(left_trimmables, 2)
        right_factor = tf_common.equal_float(right_trimmables, 1) * trim_short_right + tf_common.equal_float(right_trimmables, 2)

        hazard_list = self.target_lams * left_factor * right_factor
        return hazard_list

    """
    SECTION: methods for helping to calculate Pr(indel | target target)
    """
    def _create_log_indel_probs(self, singletons: List[Singleton]):
        """
        Create tensorflow objects for the cond prob of indels

        @return list of tensorflow tensors with indel probs for each singleton
        """
        if not singletons:
            return []
        else:
            log_insert_probs = self._create_log_insert_probs(singletons)
            log_del_probs = self._create_log_del_probs(singletons)
            log_indel_probs = log_del_probs + log_insert_probs
            return log_indel_probs

    def _create_log_del_probs(self, singletons: List[Singleton]):
        """
        Creates tensorflow nodes that calculate the log conditional probability of the deletions found in
        each of the singletons

        @return List[tensorflow nodes] for each singleton in `singletons`
        """
        min_targets = [sg.min_target for sg in singletons]
        max_targets = [sg.max_target for sg in singletons]
        is_left_longs = tf.constant(
                [sg.is_left_long for sg in singletons], dtype=tf.float64)
        is_right_longs = tf.constant(
                [sg.is_right_long for sg in singletons], dtype=tf.float64)
        start_posns = tf.constant(
                [sg.start_pos for sg in singletons], dtype=tf.float64)
        del_ends = tf.constant(
                [sg.del_end for sg in singletons], dtype=tf.float64)
        del_len = del_ends - start_posns

        # Compute conditional prob of deletion for a singleton
        min_target_sites = tf.constant([self.bcode_meta.abs_cut_sites[mt] for mt in min_targets], dtype=tf.float64)
        max_target_sites = tf.constant([self.bcode_meta.abs_cut_sites[mt] for mt in max_targets], dtype=tf.float64)
        left_trim_len = min_target_sites - start_posns
        right_trim_len = del_ends - max_target_sites

        left_trim_long_min = tf.constant([self.bcode_meta.left_long_trim_min[mt] for mt in min_targets], dtype=tf.float64)
        right_trim_long_min = tf.constant([self.bcode_meta.right_long_trim_min[mt] for mt in max_targets], dtype=tf.float64)
        left_trim_long_max = tf.constant([self.bcode_meta.left_max_trim[mt] for mt in min_targets], dtype=tf.float64)
        right_trim_long_max = tf.constant([self.bcode_meta.right_max_trim[mt] for mt in max_targets], dtype=tf.float64)

        min_left_trim = is_left_longs * left_trim_long_min
        max_left_trim = tf_common.ifelse(is_left_longs, left_trim_long_max, left_trim_long_min - 1)
        min_right_trim = is_right_longs * right_trim_long_min
        max_right_trim = tf_common.ifelse(is_right_longs, right_trim_long_max, right_trim_long_min - 1)

        check_left_max = tf.cast(tf.less_equal(left_trim_len, max_left_trim), tf.float64)
        check_left_min = tf.cast(tf.less_equal(min_left_trim, left_trim_len), tf.float64)
        left_prob = check_left_max * check_left_min * tf_common.ifelse(
                tf_common.equal_float(left_trim_len, 0),
                self.poiss_left.prob(tf.constant(0, dtype=tf.float64)) + tf.constant(1, dtype=tf.float64) - self.poiss_left.cdf(max_left_trim),
                self.poiss_left.prob(left_trim_len))
        check_right_max = tf.cast(tf.less_equal(right_trim_len, max_right_trim), tf.float64)
        check_right_min = tf.cast(tf.less_equal(min_right_trim, right_trim_len), tf.float64)
        right_prob = check_right_max * check_right_min * tf_common.ifelse(
                tf_common.equal_float(right_trim_len, 0),
                self.poiss_right.prob(tf.constant(0, dtype=tf.float64)) + tf.constant(1, dtype=tf.float64) - self.poiss_right.cdf(max_right_trim),
                self.poiss_right.prob(right_trim_len))

        lr_prob = left_prob * right_prob
        is_short_indel = tf_common.equal_float(is_left_longs + is_right_longs, 0)
        is_len_zero = tf_common.equal_float(del_len, 0)
        del_prob = tf_common.ifelse(is_short_indel,
                tf_common.ifelse(is_len_zero,
                    self.trim_zero_prob + (1.0 - self.trim_zero_prob) * lr_prob,
                    (1.0 - self.trim_zero_prob) * lr_prob),
                lr_prob)
        return tf.log(del_prob)

    def _create_log_insert_probs(self, singletons: List[Singleton]):
        """
        Creates tensorflow nodes that calculate the log conditional probability of the insertions found in
        each of the singletons

        @return List[tensorflow nodes] for each singleton in `singletons`
        """
        insert_lens = tf.constant(
                [sg.insert_len for sg in singletons], dtype=tf.float64)
        insert_len_prob = self.poiss_insert.prob(insert_lens)
        # Equal prob of all same length sequences
        insert_seq_prob = 1.0/tf.pow(tf.constant(4.0, dtype=tf.float64), insert_lens)
        is_insert_zero = tf.cast(tf.equal(insert_lens, 0), dtype=tf.float64)
        insert_prob = tf_common.ifelse(
                is_insert_zero,
                self.insert_zero_prob + (1 - self.insert_zero_prob) * insert_len_prob * insert_seq_prob,
                (1 - self.insert_zero_prob) * insert_len_prob * insert_seq_prob)
        return tf.log(insert_prob)

    """
    LOG LIKELIHOOD CALCULATION section
    """
    def create_log_lik(self, transition_wrappers: Dict):
        """
        Creates tensorflow nodes that calculate the log likelihood of the observed data
        """
        self.log_lik_cell_type = tf.zeros([])
        self.create_topology_log_lik(transition_wrappers)
        if self.cell_type_tree is None:
            self.log_lik = self.log_lik_alleles
        else:
            self.create_cell_type_log_lik()
            self.log_lik = self.log_lik_cell_type + self.log_lik_alleles

        # penalize the leaf branch lengths if they get too close to zero
        # (preventing things from going negative)
        self.branch_log_barr = tf.reduce_sum(tf.log(tf.gather(
            self.branch_lens,
            indices = [leaf.node_id for leaf in self.topology])))
        self.smooth_log_lik = tf.add(
                self.log_lik,
                self.log_barr_ph * self.branch_log_barr)

        self.smooth_log_lik_grad = self.adam_opt.compute_gradients(
            self.smooth_log_lik,
            var_list=[self.all_vars])
        self.adam_train_op = self.adam_opt.minimize(-self.smooth_log_lik, var_list=self.all_vars)

    """
    Section for creating the log likelihood of the allele data
    """
    def create_topology_log_lik(self, transition_wrappers: Dict[int, List[TransitionWrapper]]):
        """
        Create a tensorflow graph of the likelihood calculation
        """
        # Get all the conditional probabilities of the trims
        # Doing it all at once to speed up computation
        singletons = CLTLikelihoodModel.get_all_singletons(self.topology)
        self.singleton_index_dict = {sg: int(i) for i, sg in enumerate(singletons)}
        self.singleton_log_cond_prob = self._create_log_indel_probs(singletons)

        # Actually create the nodes for calculating the log likelihoods of the alleles
        self.log_lik_alleles_list = []
        self.Ddiags_list = []
        for bcode_idx in range(self.bcode_meta.num_barcodes):
            log_lik_alleles, Ddiags = self._create_topology_log_lik_barcode(transition_wrappers, bcode_idx)
            self.log_lik_alleles_list.append(log_lik_alleles)
            self.Ddiags_list.append(Ddiags)
        self.log_lik_alleles = tf.add_n(self.log_lik_alleles_list)

    def _initialize_lower_prob(self,
            transition_wrappers: Dict[int, List[TransitionWrapper]],
            node: CellLineageTree,
            bcode_idx: int):
        """
        Initialize the Lprob element with the first part of the product
            For unresolved multifurcs, this is the probability of staying in this same ancestral state (the spine's probability)
                for root nodes, this returns a scalar.
                for non-root nodes, this returns a vector with the initial value for all ancestral states under consideration.
            For resolved multifurcs, this is one
        """
        if not node.is_resolved_multifurcation():
            # Then we need to multiply the probability of the "spine" -- assuming constant ancestral state along the entire spine
            time_stays_constant = tf.reduce_max(tf.stack([
                self.branch_len_offsets[child.node_id]
                for child in node.children]))
            if not node.is_root():
                # When making this probability, order the elements per the transition matrix of this node
                transition_wrapper = transition_wrappers[node.node_id][bcode_idx]
                index_vals = [[
                        [transition_wrapper.key_dict[state], 0],
                        self.hazard_away_dict[state]]
                    for state in transition_wrapper.states]
                haz_aways = tf_common.scatter_nd(
                        index_vals,
                        output_shape=[transition_wrapper.num_possible_states + 1, 1],
                        name="haz_away.multifurc")
                return tf.exp(-haz_aways * time_stays_constant)
            else:
                root_haz_away = self.hazard_away_dict[TargetStatus()]
                return tf.exp(-root_haz_away * time_stays_constant)
        else:
            return tf.constant(1.0, dtype=tf.float64)

    def _create_topology_log_lik_barcode(
            self,
            transition_wrappers: Dict[int, List[TransitionWrapper]],
            bcode_idx: int):
        """
        @param transition_wrappers: dictionary mapping node id to list of TransitionWrapper -- carries useful information
                                    for deciding how to calculate the transition probabilities
        @param bcode_idx: the index of the allele we are calculating the likelihood for

        @return tensorflow tensor with the log likelihood of the allele with index `bcode_idx` for the given tree topology
        """
        # Store the tensorflow objects that calculate the prob of a node being in each state given the leaves
        Lprob = dict()
        Ddiags = dict()
        pt_matrix = dict()
        trans_mats = dict()
        trim_probs = dict()
        # Store all the scaling terms addressing numerical underflow
        log_scaling_terms = dict()
        # Tree traversal order should be postorder
        for node in self.topology.traverse("postorder"):
            if node.is_leaf():
                node_wrapper = transition_wrappers[node.node_id][bcode_idx]
                prob_array = np.zeros((node_wrapper.num_possible_states + 1, 1))
                observed_key = node_wrapper.key_dict[node_wrapper.leaf_state]
                prob_array[observed_key] = 1
                Lprob[node.node_id] = tf.constant(prob_array, dtype=tf.float64)
            else:
                log_Lprob_node = tf.log(self._initialize_lower_prob(
                        transition_wrappers,
                        node,
                        bcode_idx))

                for child in node.children:
                    child_wrapper = transition_wrappers[child.node_id][bcode_idx]
                    with tf.name_scope("Transition_matrix%d" % node.node_id):
                        trans_mats[child.node_id] = self._create_transition_matrix(
                                child_wrapper)

                    # Get the trim probabilities
                    with tf.name_scope("trim_matrix%d" % node.node_id):
                        trim_probs[child.node_id] = self._create_trim_prob_matrix(child_wrapper)

                    # Create the probability matrix exp(Qt)
                    with tf.name_scope("expm_ops%d" % node.node_id):
                        pt_matrix[child.node_id], _, _, Ddiags[child.node_id] = tf_common.myexpm(
                                trans_mats[child.node_id],
                                self.branch_lens[child.node_id])

                    # Get the probability for the data descended from the child node, assuming that the node
                    # has a particular target tract repr.
                    # These down probs are ordered according to the child node's numbering of the TTs states
                    with tf.name_scope("recurse%d" % node.node_id):
                        ch_ordered_down_probs = tf.matmul(
                                tf.multiply(pt_matrix[child.node_id], trim_probs[child.node_id]),
                                Lprob[child.node_id])

                    with tf.name_scope("rearrange%d" % node.node_id):
                        if not node.is_root():
                            # Reorder summands according to node's numbering of tract_repr states
                            node_wrapper = transition_wrappers[node.node_id][bcode_idx]

                            down_probs = CLTLikelihoodModel._reorder_likelihoods(
                                    ch_ordered_down_probs,
                                    node_wrapper,
                                    child_wrapper)
                        else:
                            # For the root node, we just want the probability where the root node is unmodified
                            # No need to reorder
                            ch_id = child_wrapper.key_dict[TargetStatus()]
                            down_probs = ch_ordered_down_probs[ch_id]
                        log_Lprob_node = log_Lprob_node + tf.log(down_probs)

                # Handle numerical underflow
                log_scaling_term = tf.reduce_max(log_Lprob_node)
                Lprob[node.node_id] = tf.exp(log_Lprob_node - log_scaling_term, name="scaled_down_prob")
                log_scaling_terms[node.node_id] = log_scaling_term

        with tf.name_scope("alleles_log_lik"):
            # Account for the scaling terms we used for handling numerical underflow
            log_scaling_terms_all = tf.stack(list(log_scaling_terms.values()))
            log_lik_alleles = tf.add(
                tf.reduce_sum(log_scaling_terms_all, name="add_normalizer"),
                tf.log(Lprob[self.root_node_id]),
                name="alleles_log_lik")

        return log_lik_alleles, Ddiags

    def _create_transition_matrix(self, transition_wrapper: TransitionWrapper):
        """
        @param transition_wrapper: TransitionWrapper that is associated with a particular branch

        @return tensorflow tensor with instantaneous transition rates between meta-states,
                only specifies rates for the meta-states given in `transition_wrapper`.
                So it will create a row for each meta-state in the `transition_wrapper` and then
                create one "impossible" sink state.
                This is the Q matrix for a given branch.
        """
        # Get the target tracts of the singletons -- this is important
        # since the transition matrix excludes the impossible target tracts
        special_tts = set([
                sgwc.get_singleton().get_target_tract()
                for sgwc in transition_wrapper.anc_state.get_singleton_wcs()])

        possible_states = set(transition_wrapper.states)
        impossible_key = transition_wrapper.num_possible_states

        index_vals = []
        for start_state in transition_wrapper.states:
            start_key = transition_wrapper.key_dict[start_state]
            haz_away = self.hazard_away_dict[start_state]

            # Hazard of staying is negative of hazard away
            index_vals.append([[start_key, start_key], -haz_away])

            all_end_states = set(self.targ_stat_transitions_dict[start_state].keys())
            possible_end_states = all_end_states.intersection(possible_states)
            haz_to_possible = 0
            for end_state in possible_end_states:
                # Figure out if this is a special transition involving only a particular target tract
                # Or if it a general any-target-tract transition
                target_tracts_for_transition = self.targ_stat_transitions_dict[start_state][end_state]
                matching_tts = special_tts.intersection(target_tracts_for_transition)
                if matching_tts:
                    assert len(matching_tts) == 1
                    matching_tt = list(matching_tts)[0]
                    hazard = self.target_tract_hazards[self.target_tract_dict[matching_tt]]
                else:
                    # if we already calculated the hazard of the transition between these target statuses,
                    # use the same node
                    if end_state in self.targ_stat_transition_hazards_dict[start_state]:
                        hazard = self.targ_stat_transition_hazards_dict[start_state][end_state]
                    else:
                        hazard = tf.add_n(
                            [self.target_tract_hazards[self.target_tract_dict[tt]] for tt in target_tracts_for_transition])

                        # Store this hazard if we need it in the future
                        self.targ_stat_transition_hazards_dict[start_state][end_state] = hazard

                end_key = transition_wrapper.key_dict[end_state]
                haz_to_possible += hazard
                index_vals.append([[start_key, end_key], hazard])

            # Hazard to unlikely state is hazard away minus hazard to likely states
            index_vals.append([[start_key, impossible_key], haz_away - haz_to_possible])

        matrix_len = transition_wrapper.num_possible_states + 1
        q_matrix = tf_common.scatter_nd(
            index_vals,
            output_shape=[matrix_len, matrix_len],
            name="top.q_matrix")
        return q_matrix

    def _create_trim_prob_matrix(self, child_transition_wrapper: TransitionWrapper):
        """
        @param transition_wrapper: TransitionWrapper that is associated with a particular branch

        @return matrix of conditional probabilities of each trim
                note: this is used to generate the matrix for a specific branch in the tree
        """
        index_vals = []

        child_singletons = child_transition_wrapper.anc_state.get_singletons()
        target_to_singleton = {
                sg.min_deact_target: sg for sg in child_singletons}
        singleton_targets = set(list(target_to_singleton.keys()))

        for start_target_status in child_transition_wrapper.states:
            for end_target_status in child_transition_wrapper.states:
                new_deact_targets = end_target_status.minus(start_target_status)
                matching_targets = new_deact_targets.intersection(singleton_targets)
                if matching_targets:
                    new_singletons = [target_to_singleton[target] for target in matching_targets]
                    log_trim_probs = tf.gather(
                            params = self.singleton_log_cond_prob,
                            indices = [self.singleton_index_dict[sg] for sg in new_singletons])
                    log_val = tf.reduce_sum(log_trim_probs)
                    start_key = child_transition_wrapper.key_dict[start_target_status]
                    end_key = child_transition_wrapper.key_dict[end_target_status]
                    index_vals.append([[start_key, end_key], log_val])

        output_shape = [child_transition_wrapper.num_possible_states + 1, child_transition_wrapper.num_possible_states + 1]
        if index_vals:
            return tf.exp(tf_common.scatter_nd(
                index_vals,
                output_shape,
                name="top.trim_probs"))
        else:
            return tf.ones(output_shape, dtype=tf.float64)

    """
    Functions for getting the log likelihood of the cell type information
    (not really used or tested recently. you are warned)
    """
    def create_cell_type_log_lik(self):
        """
        Create a tensorflow graph of the likelihood calculation
        """
        self.cell_type_q_mat = self._create_cell_type_instant_matrix()
        # Store the tensorflow objects that calculate the prob of a node being in each state given the leaves
        self.L_cell_type = dict()
        self.D_cell_type = dict()
        # Store all the scaling terms addressing numerical underflow
        scaling_terms = []
        # Traversal needs to be postorder!
        for node in self.topology.traverse("postorder"):
            if node.is_leaf():
                cell_type_one_hot = np.zeros((self.num_cell_types, 1))
                cell_type_one_hot[node.cell_state.categorical_state.cell_type] = 1
                self.L_cell_type[node.node_id] = tf.constant(cell_type_one_hot, dtype=tf.float64)
            else:
                self.L_cell_type[node.node_id] = tf.constant(1.0, dtype=tf.float64)
                for child in node.children:
                    # Create the probability matrix exp(Qt) = A * exp(Dt) * A^-1
                    with tf.name_scope("cell_type_expm_ops%d" % node.node_id):
                        pr_matrix, _, _, D = tf_common.myexpm(
                                self.cell_type_q_mat,
                                self.branch_lens[child.node_id])
                        self.D_cell_type[child.node_id] = D
                        down_probs = tf.matmul(pr_matrix, self.L_cell_type[child.node_id])
                        self.L_cell_type[node.node_id] = tf.multiply(
                                self.L_cell_type[node.node_id],
                                down_probs)

                if node.is_leaf():
                    # If node is observed, we don't need all the other values in this vector
                    # TODO: we might be doing extra computation, though it seems okay for now
                    node_observe_state = node.cell_state.categorical_state.cell_type
                    cell_type_one_hot = np.zeros((self.num_cell_types, 1))
                    cell_type_one_hot[node_observe_state] = 1
                    self.L_cell_type[node.node_id] = tf.multiply(
                            self.L_cell_type[node.node_id][node_observe_state],
                            tf.constant(cell_type_one_hot, dtype=tf.float64))

                # Handle numerical underflow
                scaling_term = tf.reduce_sum(self.L_cell_type[node.node_id], name="scaling_term")
                self.L_cell_type[node.node_id] = tf.div(self.L_cell_type[node.node_id], scaling_term, name="sub_log_lik")
                scaling_terms.append(scaling_term)

        with tf.name_scope("cell_type_log_lik"):
            # Account for the scaling terms we used for handling numerical underflow
            scaling_terms = tf.stack(scaling_terms)
            self.log_lik_cell_type = tf.add(
                tf.reduce_sum(tf.log(scaling_terms), name="add_normalizer"),
                tf.log(self.L_cell_type[self.root_node_id][self.cell_type_root]),
                name="cell_type_log_lik")

    def _create_cell_type_instant_matrix(self, haz_away=1e-10):
        num_leaves = tf.constant(len(self.cell_type_tree), dtype=tf.float64)
        index_vals = []
        self.num_cell_types = 0
        # Note: tree traversal order doesnt matter
        for node in self.cell_type_tree.traverse("preorder"):
            self.num_cell_types += 1
            if node.is_leaf():
                haz_node = haz_away + np.random.rand() * 1e-10
                haz_node_away = tf.constant(haz_node, dtype=tf.float64)
                index_vals.append([(node.cell_type, node.cell_type), -haz_away])
                for leaf in self.cell_type_tree:
                    if leaf.cell_type != node.cell_type:
                        index_vals.append([(node.cell_type, leaf.cell_type), haz_node_away/(num_leaves - 1)])
            else:
                tot_haz = tf.zeros([], dtype=tf.float64)
                for child in node.get_children():
                    haz_child = self.cell_type_lams[child.cell_type]
                    index_vals.append([(node.cell_type, child.cell_type), haz_child])
                    tot_haz = tf.add(tot_haz, haz_child)
                index_vals.append([(node.cell_type, node.cell_type), -tot_haz])

        q_matrix = tf_common.scatter_nd(
                index_vals,
                output_shape=[self.num_cell_types, self.num_cell_types],
                name="top.cell_type_q_matrix")
        return q_matrix

    def get_fitted_bifurcating_tree(self):
        """
        Recall the model was parameterized as a continuous formulation for a multifurcating tree.
        This function returns the bifurcating tree topology using the current model parameters.
        @return CellLineageTree
        """
        # Get the current model parameters
        br_lens, br_len_offsets = self.sess.run(
                [self.branch_lens, self.branch_len_offsets],
                feed_dict={self.tot_time_ph: self.tot_time})

        scratch_tree = self.topology.copy("deepcopy")
        for node in scratch_tree.traverse("preorder"):
            if not node.is_resolved_multifurcation():
                # Resolve the multifurcation by creating the spine of "identifcal" nodes
                children = node.get_children()
                children_offsets = [br_len_offsets[c.node_id] for c in children]
                sort_indexes = np.argsort(children_offsets)

                curr_offset = 0
                curr_spine_node = node
                for idx in sort_indexes:
                    new_spine_node = CellLineageTree(
                            allele_list = node.allele_list,
                            allele_events_list = node.allele_events_list,
                            cell_state = node.cell_state,
                            dist = children_offsets[idx] - curr_offset)
                    new_spine_node.node_id = None
                    curr_spine_node.add_child(new_spine_node)

                    child = children[idx]
                    node.remove_child(child)
                    new_spine_node.add_child(child)

                    curr_spine_node = new_spine_node
                    curr_offset = children_offsets[idx]

            if node.is_root():
                node.dist = 0
            elif node.node_id is not None:
                node.dist = br_lens[node.node_id]

        # Just checking that the tree is ultrametric
        for leaf in scratch_tree:
            assert np.isclose(self.tot_time, leaf.get_distance(scratch_tree))
        return scratch_tree

    @staticmethod
    def get_all_singletons(topology: CellLineageTree):
        singletons = set()
        for leaf in topology:
            for leaf_anc_state in leaf.anc_state_list:
                for singleton_wc in leaf_anc_state.indel_set_list:
                    sg = singleton_wc.get_singleton()
                    singletons.add(sg)
        return singletons

    @staticmethod
    def _reorder_likelihoods(
            ordered_down_probs: Tensor,
            new_wrapper: TransitionWrapper,
            old_wrapper: TransitionWrapper):
        """
        @param ch_ordered_down_probs: the Tensorflow array to be re-ordered
        @param tract_repr_list: list of target tract reprs to include in the vector
                        rest can be set to zero
        @param node_trans_mat: provides the desired ordering
        @param ch_trans_mat: provides the ordering used in vec_lik

        @return the reordered version of vec_lik according to the order in node_trans_mat
        """
        index_vals = [[
                [new_wrapper.key_dict[targ_stat], 0],
                ordered_down_probs[old_wrapper.key_dict[targ_stat]][0]]
            for targ_stat in new_wrapper.states]
        down_probs = tf_common.scatter_nd(
                index_vals,
                output_shape=[new_wrapper.num_possible_states + 1, 1],
                name="top.down_probs")
        return down_probs

    """
    Logger creating/closing functions for debugging
    """
    def create_logger(self):
        self.profile_writer = tf.summary.FileWriter("_output", self.sess.graph)

    def close_logger(self):
        self.profile_writer.close()

    """
    DEBUGGING CODE -- this is for checking the gradient
    """
    def get_log_lik(self, get_grad: bool=False, do_logging: bool=False):
        """
        @return the log likelihood and the gradient, if requested
        """
        feed_dict = {self.tot_time_ph: self.tot_time}
        if get_grad and not do_logging:
            log_lik, grad = self.sess.run([self.log_lik, self.log_lik_grad], feed_dict=feed_dict)
            return log_lik, grad[0][0]
        elif do_logging:
            # For tensorflow logging
            run_options = tf.RunOptions(trace_level=tf.RunOptions.FULL_TRACE)
            run_metadata = tf.RunMetadata()

            # For my own checks on the d matrix and q matrix
            dkey_list = list(self.D.keys())
            D_vals = [self.D[k] for k in dkey_list]
            trans_mats_vals = [self.trans_mats[k] for k in dkey_list]
            D_cell_type_vals = [self.D_cell_type[k] for k in list(self.D_cell_type.keys())]

            if get_grad:
                log_lik, grad, Ds, q_mats, D_types = self.sess.run(
                        [self.log_lik, self.log_lik_grad, D_vals, trans_mats_vals, D_cell_type_vals],
                        feed_dict = feed_dict,
                        options=run_options,
                        run_metadata=run_metadata)
                grad = grad[0][0]
            else:
                log_lik, Ds, q_mats, D_types = self.sess.run(
                        [self.log_lik, D_vals, trans_mats_vals, D_cell_type_vals],
                        feed_dict = feed_dict,
                        options=run_options,
                        run_metadata=run_metadata)
                grad = None

            # Profile computation time in tensorflow
            self.profile_writer.add_run_metadata(run_metadata, "get_log_lik")

            # Quick check that all the diagonal matrix from the eigendecomp were unique
            for d, q in zip(Ds, q_mats):
                d_size = d.size
                uniq_d = np.unique(d)
                if uniq_d.size != d_size:
                    print("Uhoh. D matrix does not have unique eigenvalues. %d vs %d" % (uniq_d.size, d_size))
                    print("Q mat", np.sort(np.diag(q)))
                    print(np.sort(np.linalg.eig(q)[0]))

            return log_lik, grad
        else:
            return self.sess.run(self.log_lik, feed_dict = feed_dict), None


    def check_grad(self, transition_matrices, epsilon=1e-10):
        """
        Function just for checking the gradient
        """
        orig_params = self.sess.run(self.all_vars)
        self.create_topology_log_lik(transition_matrices)
        log_lik, grad = self.get_log_lik(get_grad=True)
        print("log lik", log_lik)
        print("all grad", grad)
        for i in range(len(orig_params)):
            new_params = np.copy(orig_params)
            new_params[i] += epsilon
            self.sess.run(self.assign_all_vars, feed_dict={self.all_vars_ph: new_params})

            log_lik_eps, _ = self.get_log_lik()
            log_lik_approx = (log_lik_eps - log_lik)/epsilon
            print("index", i, " -- LOG LIK GRAD APPROX", log_lik_approx)
            print("index", i, " --                GRAD ", grad[i])
