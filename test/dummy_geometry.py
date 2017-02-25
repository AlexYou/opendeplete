""" The OpenMC wrapper module.

This module implements the OpenDeplete -> OpenMC linkage.
"""

import numpy as np
import scipy.sparse as sp

from opendeplete.reaction_rates import ReactionRates
from opendeplete.function import Operator

class DummyGeometry(Operator):
    """ This is a dummy geometry class with no statistical uncertainty.

    y_1' = sin(y_2) y_1 + cos(y_1) y_2
    y_2' = -cos(y_2) y_1 + sin(y_1) y_2

    y_1(0) = 1
    y_2(0) = 1

    y_1(1.5) ~ 2.3197067076743316
    y_2(1.5) ~ 3.1726475740397628

    """

    def __init__(self, settings):
        Operator.__init__(self, settings)

    def eval(self, vec, print_out=False):
        """ Evaluates F(y)

        Parameters
        ----------
        vec : list of numpy.array
            Total atoms to be used in function.
        print_out : bool, optional, ignored
            Whether or not to print out time.

        Returns
        -------
        mat : list of scipy.sparse.csr_matrix
            Matrices for the next step.
        k : float
            Zero.
        rates : ReactionRates
            Reaction rates from this simulation.
        seed : int
            Zero.
        """

        y_1 = vec[0][0]
        y_2 = vec[0][1]

        mat = np.zeros((2, 2))
        a11 = np.sin(y_2)
        a12 = np.cos(y_1)
        a21 = -np.cos(y_2)
        a22 = np.sin(y_1)

        mat = [sp.csr_matrix(np.array([[a11, a12], [a21, a22]]))]

        # Create a fake rates object

        return mat, 0.0, self.reaction_rates, 0

    @property
    def volume(self):
        """
        volume : list of float
            Volume for a material.
        """

        volume = [0.0]

        return volume

    @property
    def nuc_list(self):
        """
        nuc_list : list of str
            A list of all nuclide names. Used for sorting the simulation.
        """

        return ["1", "2"]

    @property
    def burn_list(self):
        """
        burn_list : list of str
            A list of all cell IDs to be burned.  Used for sorting the simulation.
        """

        return ["1"]

    @property
    def reaction_rates(self):
        """
        reaction_rates : ReactionRates
            Reaction rates from the last operator step.
        """
        cell_to_ind = {"1" : 0}
        nuc_to_ind = {"1" : 0, "2" : 1}
        react_to_ind = {"1" : 0}

        return ReactionRates(cell_to_ind, nuc_to_ind, react_to_ind)

    def initial_condition(self):
        """ Returns initial vector.

        Returns
        -------
        list of numpy.array
            Total density for initial conditions.
        """

        return [np.array((1.0, 1.0))]

    def get_results_info(self):
        """ Returns volume list, cell lists, and nuc lists.

        Returns
        -------
        volume : list of float
            Volumes corresponding to materials in burn_list
        nuc_list : list of str
            A list of all nuclide names. Used for sorting the simulation.
        burn_list : list of int
            A list of all cell IDs to be burned.  Used for sorting the simulation.
        """

        return self.volume, self.nuc_list, self.burn_list
