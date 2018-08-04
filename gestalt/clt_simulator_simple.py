import numpy as np

from cell_lineage_tree import CellLineageTree
from clt_simulator import CLTSimulator


class CLTSimulatorSimple(CLTSimulator):
    """
    Creates a cell lineage tree that is a cherry.
    Then it simulates the alleles on this cherry.
    A CHERRY TREE!
    """
    def simulate(self,
            tree_seed: int,
            data_seed: int,
            tot_time: float,
            max_nodes: int = 10):
        np.random.seed(tree_seed)
        root_allele = self.allele_simulator.get_root()
        root_cell_state = self.cell_state_simulator.get_root()

        child_dist = np.random.rand() * tot_time

        tree = CellLineageTree(
            allele_list=root_allele,
            cell_state=root_cell_state,
            dist=0)
        child = CellLineageTree(
            allele_list=root_allele,
            cell_state=root_cell_state,
            dist=child_dist)
        leaf1 = CellLineageTree(
            allele_list=root_allele,
            cell_state=root_cell_state,
            dist=tot_time - child_dist)
        leaf2 = CellLineageTree(
            allele_list=root_allele,
            cell_state=root_cell_state,
            dist=tot_time - child_dist)
        tree.add_child(child)
        child.add_child(leaf1)
        child.add_child(leaf2)

        np.random.seed(data_seed)
        # Run the simulation to create the alleles along the tree topology
        self._simulate_alleles(tree)
        self._simulate_cell_states(tree)
        return tree