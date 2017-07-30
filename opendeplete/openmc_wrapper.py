""" The OpenMC wrapper module.

This module implements the OpenDeplete -> OpenMC linkage.
"""

import copy
from collections import OrderedDict
import os
import random
import sys
import time
try:
    import lxml.etree as ET
    _have_lxml = True
except ImportError:
    import xml.etree.ElementTree as ET
    from openmc.clean_xml import clean_xml_indentation
    _have_lxml = False

import h5py
from mpi4py import MPI
import numpy as np
import openmc

from .atom_number import AtomNumber
from .depletion_chain import DepletionChain
from .reaction_rates import ReactionRates
from .function import Settings, Operator


def chunks(items, n):
    min_size, extra = divmod(len(items), n)
    j = 0
    chunk_list = []
    for i in range(n):
        chunk_size = min_size + int(i < extra)
        chunk_list.append(items[j:j + chunk_size])
        j += chunk_size
    return chunk_list


class OpenMCSettings(Settings):
    """ The OpenMCSettings class.

    Extends Settings to provide information OpenMC needs to run.

    Attributes
    ----------
    dt_vec : numpy.array
        Array of time steps to take. (From Settings)
    tol : float
        Tolerance for adaptive time stepping. (From Settings)
    output_dir : str
        Path to output directory to save results. (From Settings)
    chain_file : str
        Path to the depletion chain xml file.  Defaults to the environment
        variable "OPENDEPLETE_CHAIN" if it exists.
    openmc_call : str
        OpenMC executable path.  Defaults to "openmc".
    openmc_npernode : int
        Number of openmc MPI ranks per node.  OpenMP will be used to fill the
        rest of the node.  Defaults to 1 for memory reasons.
    particles : int
        Number of particles to simulate per batch.
    batches : int
        Number of batches.
    inactive : int
        Number of inactive batches.
    lower_left : list of float
        Coordinate of lower left of bounding box of geometry.
    upper_right : list of float
        Coordinate of upper right of bounding box of geometry.
    entropy_dimension : list of int
        Grid size of entropy.
    dilute_initial : float, default 1.0e3
        Initial atom density to add for nuclides that are zero in initial
        condition to ensure they exist in the decay chain.  Only done for
        nuclides with reaction rates.
    round_number : bool
        Whether or not to round output to OpenMC to 8 digits.
        Useful in testing, as OpenMC is incredibly sensitive to exact values.
    constant_seed : int
        If present, all runs will be performed with this seed.
    power : float
        Power of the reactor (currently in MeV/second-cm).
    """

    def __init__(self):
        super().__init__()
        # OpenMC specific
        try:
            self.chain_file = os.environ["OPENDEPLETE_CHAIN"]
        except KeyError:
            self.chain_file = None
        self.openmc_call = "openmc"
        self.openmc_npernode = 1
        self.particles = None
        self.batches = None
        self.inactive = None
        self.lower_left = None
        self.upper_right = None
        self.entropy_dimension = None
        self.dilute_initial = 1.0e3

        # OpenMC testing specific
        self.round_number = False
        self.constant_seed = None

        # Depletion problem specific
        self.power = None


class Materials(object):
    """The Materials class.

    Contains information about cross sections for a cell.

    Attributes
    ----------
    temperature : float
        Temperature in Kelvin for each region.
    sab : str or list of str
        ENDF S(a,b) name for a region that needs S(a,b) data.  Not set if no
        S(a,b) needed for region.
    """

    def __init__(self):
        self.temperature = None
        self.sab = None


class OpenMCOperator(Operator):
    """The OpenMC Operator class.

    Provides Operator functions for OpenMC.

    Parameters
    ----------
    geometry : openmc.Geometry
        The OpenMC geometry object.
    settings : OpenMCSettings
        Settings object.

    Attributes
    ----------
    settings : OpenMCSettings
        Settings object. (From Operator)
    geometry : openmc.Geometry
        The OpenMC geometry object.
    materials : list of Materials
        Materials to be used for this simulation.
    seed : int
        The RNG seed used in last OpenMC run.
    number : AtomNumber
        Total number of atoms in simulation.
    participating_nuclides : set of str
        A set listing all unique nuclides available from cross_sections.xml.
    chain : DepletionChain
        The depletion chain information necessary to form matrices and tallies.
    reaction_rates : ReactionRates
        Reaction rates from the last operator step.
    power : OrderedDict of str to float
        Material-by-Material power.  Indexed by material ID.
    mat_name : OrderedDict of str to int
        The name of region each material is set to.  Indexed by material ID.
    burn_mat_to_id : OrderedDict of str to int
        Dictionary mapping material ID (as a string) to an index in reaction_rates.
    burn_nuc_to_id : OrderedDict of str to int
        Dictionary mapping nuclide name (as a string) to an index in
        reaction_rates.
    n_nuc : int
        Number of nuclides considered in the decay chain.
    comm : MPI.COMM_WORLD
        The mpi communicator.
    rank : int
        MPI rank of this object.
    size : int
        The number of MPI threads.
    nodes : int
        An approximate quantity of nodes.
    npernode : int
        An approximate quantity of processes per node.  If the architecture is
        heterogeneous, takes the smallest number of processes per node.  This
        assumes that OpenDeplete is running on all nodes.
    mat_tally_ind : OrderedDict of str to int
        Dictionary mapping material ID to index in tally.
    """

    def __init__(self, geometry, settings):
        super().__init__(settings)

        self.comm = MPI.COMM_WORLD

        self.rank = self.comm.rank
        self.size = self.comm.size

        self.nodes = 0
        self.npernode = 0

        # Compute number of nodes and size of each node.
        subcomm = self.comm.Split_type(MPI.COMM_TYPE_SHARED)
        if self.rank == 0:
            sub_rank = np.zeros(self.size, np.int32)
            for i in range(1, self.size):
                sub_rank[i] = self.comm.recv(source=i, tag=0)

            # Compute the quantity of each rank
            quantity = np.zeros(np.max(sub_rank) + 1, np.int32)
            for i in range(self.size):
                quantity[sub_rank[i]] += 1

            # Starting from the bottom, compute the largest "block"
            j = 1
            for i in range(1, len(quantity)):
                if quantity[i-1] != quantity[i]:
                    break
                else:
                    j = i + 1
            self.nodes = quantity[j-1]
            self.npernode = j
        else:
            self.comm.send(subcomm.rank, dest=0, tag=0)

        self.nodes = self.comm.bcast(self.nodes, root=0)
        self.npernode = self.comm.bcast(self.npernode, root=0)
        self.comm.barrier()

        self.geometry = geometry
        self.materials = []
        self.seed = 0
        self.number = None
        self.participating_nuclides = None
        self.reaction_rates = None
        self.power = None
        self.mat_name = OrderedDict()
        self.burn_mat_to_ind = OrderedDict()
        self.burn_nuc_to_ind = None

        # Read depletion chain
        self.chain = DepletionChain.xml_read(settings.chain_file)

        # Clear out OpenMC, create task lists, distribute
        if self.rank == 0:
            clean_up_openmc()
            mat_burn_lists, \
                mat_not_burn_lists, \
                volume, \
                self.mat_tally_ind, \
                nuc_dict = self.extract_mat_ids()

            mat_burn = mat_burn_lists[0]
            mat_not_burn = mat_not_burn_lists[0]

            # Send assignments to all
            for i in range(1, self.size):
                self.comm.send(mat_burn_lists[i], dest=i, tag=0)
                self.comm.send(mat_not_burn_lists[i], dest=i, tag=1)
                self.comm.send(nuc_dict, dest=i, tag=2)
        else:
            # Receive assignments
            mat_burn = self.comm.recv(source=0, tag=0)
            mat_not_burn = self.comm.recv(source=0, tag=1)
            nuc_dict = self.comm.recv(source=0, tag=2)
            volume = None
            self.mat_tally_ind = None

        volume = self.comm.bcast(volume, root=0)
        self.mat_tally_ind = self.comm.bcast(self.mat_tally_ind, root=0)

        # Load participating nuclides
        self.load_participating()

        # Extract number densities from the geometry
        self.extract_number(mat_burn, mat_not_burn, volume, nuc_dict)

        # Create reaction rate tables
        self.initialize_reaction_rates()

        # Create XML files
        if self.rank == 0:
            self.geometry.export_to_xml()
        self.generate_settings_xml()
        self.generate_materials_xml()
        self.generate_tally_xml()

        # Initialize OpenMC library
        openmc.capi.init(self.comm)

    def __del__(self):
        openmc.capi.finalize()

    def extract_mat_ids(self):
        """ Extracts materials and assigns them to processes.

        Returns
        -------
        mat_burn_lists : list of list of int
            List of burnable materials indexed by rank.
        mat_not_burn_lists : list of list of int
            List of non-burnable materials indexed by rank.
        volume : OrderedDict of str to float
            Volume of each cell
        mat_tally_ind : OrderedDict of str to int
            Dictionary mapping material ID to index in tally.
        nuc_dict : OrderedDict of str to int
            Nuclides in order of how they'll appear in the simulation.
        """

        mat_burn = set()
        mat_not_burn = set()
        nuc_set = set()

        volume = OrderedDict()

        # Iterate once through the geometry to get dictionaries
        cells = self.geometry.get_all_material_cells()
        for cell in cells.values():
            name = cell.name

            if isinstance(cell.fill, openmc.Material):
                mat = cell.fill
                for nuclide in mat.get_nuclide_densities():
                    nuc_set.add(nuclide)
                if mat.depletable:
                    mat_burn.add(str(mat.id))
                    volume[str(mat.id)] = mat.volume
                else:
                    mat_not_burn.add(str(mat.id))
                self.mat_name[mat.id] = name
            else:
                for mat in cell.fill:
                    for nuclide in mat.get_nuclide_densities():
                        nuc_set.add(nuclide)
                    if mat.depletable:
                        mat_burn.add(str(mat.id))
                        volume[str(mat.id)] = mat.volume
                    else:
                        mat_not_burn.add(str(mat.id))
                    self.mat_name[mat.id] = name

        need_vol = []

        for mat_id in volume:
            if volume[mat_id] is None:
                need_vol.append(mat_id)

        if need_vol:
            exit("Need volumes for materials: " + str(need_vol))

        # Sort the sets
        mat_burn = sorted(mat_burn, key=int)
        mat_not_burn = sorted(mat_not_burn, key=int)
        nuc_set = sorted(nuc_set)

        # Construct a global nuclide dictionary, burned first
        nuc_dict = copy.deepcopy(self.chain.nuclide_dict)

        i = len(nuc_dict)

        for nuc in nuc_set:
            if nuc not in nuc_dict:
                nuc_dict[nuc] = i
                i += 1

        # Decompose geometry
        mat_burn_lists = chunks(mat_burn, self.size)
        mat_not_burn_lists = chunks(mat_not_burn, self.size)

        mat_tally_ind = OrderedDict()

        for i, mat in enumerate(mat_burn):
            mat_tally_ind[mat] = i

        return mat_burn_lists, mat_not_burn_lists, volume, mat_tally_ind, nuc_dict

    def extract_number(self, mat_burn, mat_not_burn, volume, nuc_dict):
        """ Construct self.number read from geometry

        Parameters
        ----------
        mat_burn : list of int
            Materials to be burned managed by this thread.
        mat_not_burn
            Materials not to be burned managed by this thread.
        volume : OrderedDict of str to float
            Volumes for the above materials.
        nuc_dict : OrderedDict of str to int
            Nuclides to be used in the simulation.
        """

        # Same with materials
        mat_dict = OrderedDict()
        self.burn_mat_to_ind = OrderedDict()
        i = 0
        for mat in mat_burn:
            mat_dict[mat] = i
            self.burn_mat_to_ind[mat] = i
            i += 1

        for mat in mat_not_burn:
            mat_dict[mat] = i
            i += 1

        n_mat_burn = len(mat_burn)
        n_nuc_burn = len(self.chain.nuclide_dict)

        self.number = AtomNumber(mat_dict, nuc_dict, volume, n_mat_burn, n_nuc_burn)

        if self.settings.dilute_initial != 0.0:
            for nuc in self.burn_nuc_to_ind:
                self.number.set_atom_density(np.s_[:], nuc, self.settings.dilute_initial)

        self.materials = [None] * self.number.n_mat

        # Now extract the number densities and store
        cells = self.geometry.get_all_material_cells()
        for cell in cells.values():
            if isinstance(cell.fill, openmc.Material):
                if str(cell.fill.id) in mat_dict:
                    self.set_number_from_mat(cell.fill)
            else:
                for mat in cell.fill:
                    if str(mat.id) in mat_dict:
                        self.set_number_from_mat(mat)

    def set_number_from_mat(self, mat):
        """ Extracts material and number densities from openmc.Material

        Parameters
        ----------
        mat : openmc.Materials
            The material to read from
        """

        mat_id = str(mat.id)
        mat_ind = self.number.mat_to_ind[mat_id]

        self.materials[mat_ind] = Materials()
        self.materials[mat_ind].sab = mat._sab
        self.materials[mat_ind].temperature = mat.temperature

        nuc_dens = mat.get_nuclide_atom_densities()
        for nuclide in nuc_dens:
            name = nuclide.name
            number = nuc_dens[nuclide][1] * 1.0e24
            self.number.set_atom_density(mat_id, name, number)

    def initialize_reaction_rates(self):
        """ Create reaction rates object. """
        self.reaction_rates = ReactionRates(
            self.burn_mat_to_ind,
            self.burn_nuc_to_ind,
            self.chain.react_to_ind)

        self.chain.nuc_to_react_ind = self.burn_nuc_to_ind

    def eval(self, vec, print_out=True):
        """ Runs a simulation.

        Parameters
        ----------
        vec : list of numpy.array
            Total atoms to be used in function.
        print_out : bool, optional
            Whether or not to print out time.

        Returns
        -------
        mat : list of scipy.sparse.csr_matrix
            Matrices for the next step.
        k : float
            Eigenvalue of the problem.
        rates : ReactionRates
            Reaction rates from this simulation.
        seed : int
            Seed for this simulation.
        """

        # Prevent OpenMC from complaining about re-creating tallies
        clean_up_openmc()

        self.comm.barrier()

        # Update status
        self.set_density(vec)


        time_start = time.time()

        # Update material compositions and tally nuclides
        self.update_materials()
        openmc.capi.tallies[1].nuclides = self._get_tally_nuclides()

        # Run OpenMC
        openmc.capi.hard_reset()
        openmc.capi.run()

        time_openmc = time.time()

        # Extract results
        k = self.unpack_tallies_and_normalize()

        if self.rank == 0:
            time_unpack = time.time()

            if print_out:
                print("Time to openmc: ", time_openmc - time_start)
                print("Time to unpack: ", time_unpack - time_openmc)

        return k, copy.deepcopy(self.reaction_rates), self.seed

    def form_matrix(self, y, mat):
        """ Forms the depletion matrix.

        Parameters
        ----------
        y : numpy.ndarray
            An array representing reaction rates for this cell.
        mat : int
            Material id.

        Returns
        -------
        scipy.sparse.csr_matrix
            Sparse matrix representing the depletion matrix.
        """

        return copy.deepcopy(self.chain.form_matrix(y[mat, :, :]))

    def initial_condition(self):
        """ Performs final setup and returns initial condition.

        Returns
        -------
        list of numpy.array
            Total density for initial conditions.
        """

        # Return number density vector
        return self.total_density_list()

    def update_materials(self):
        number_list = self.comm.allgather(self.number)

        for number_i in number_list:
            for mat in number_i.mat_to_ind:
                nuclides = []
                densities = []
                for nuc in self.number.nuc_to_ind:
                    if nuc in self.participating_nuclides:
                        val = 1.0e-24*self.number.get_atom_density(mat, nuc)

                        # If nuclide is zero, do not add to the problem.
                        if val > 0.0:
                            if self.settings.round_number:
                                val_magnitude = np.floor(np.log10(val))
                                val_scaled = val / 10**val_magnitude
                                val_round = round(val_scaled, 8)

                                val = val_round * 10**val_magnitude

                            nuclides.append(nuc)
                            densities.append(val)
                        else:
                            # Only output warnings if values are significantly
                            # negative.  CRAM does not guarantee positive values.
                            if val < -1.0e-21:
                                print("WARNING: nuclide ", nuc, " in material ", mat,
                                      " is negative (density = ", val, " at/barn-cm)")
                            number_i[mat, nuc] = 0.0

                mat_view = openmc.capi.materials[int(mat)]
                mat_view.set_densities(nuclides, densities)

    def generate_materials_xml(self):
        """ Creates materials.xml from self.number.

        Due to uncertainty with how MPI interacts with OpenMC API, this
        constructs the XML manually.  The long term goal is to do this
        either through PHDF5 or direct memory writing.
        """

        xml_strings = []

        for mat in self.number.mat_to_ind:
            root = ET.Element("material")
            root.set("id", mat)

            density = ET.SubElement(root, "density")
            density.set("units", "sum")

            temperature = ET.SubElement(root, "temperature")
            mat_id = self.number.mat_to_ind[mat]
            temperature.text = str(self.materials[mat_id].temperature)

            for nuc in self.number.nuc_to_ind:
                if nuc in self.participating_nuclides:
                    val = 1.0e-24*self.number.get_atom_density(mat, nuc)

                    # If nuclide is zero, do not add to the problem.
                    if val > 0.0:
                        if self.settings.round_number:
                            val_magnitude = np.floor(np.log10(val))
                            val_scaled = val / 10**val_magnitude
                            val_round = round(val_scaled, 8)

                            val = val_round * 10**val_magnitude

                        nuc_element = ET.SubElement(root, "nuclide")
                        nuc_element.set("ao", str(val))
                        nuc_element.set("name", nuc)
                    else:
                        # Only output warnings if values are significantly
                        # negative.  CRAM does not guarantee positive values.
                        if val < -1.0e-21:
                            print("WARNING: nuclide ", nuc, " in material ", mat,
                                  " is negative (density = ", val, " at/barn-cm)")
                        self.number[mat, nuc] = 0.0

            for sab in self.materials[mat_id].sab:
                sab_el = ET.SubElement(root, "sab")
                sab_el.set("name", sab[0])
                if sab[1] != 1.0:
                    sab_el.set("fraction", str(sab[1]))

            if _have_lxml:
                fragment = ET.tostring(root, encoding="unicode", pretty_print="true")
                xml_strings.append(fragment)
            else:
                clean_xml_indentation(root, spaces_per_level=2)
                fragment = ET.tostring(root, encoding="unicode", pretty_print="true")
                xml_strings.append(fragment)

        xml_string = "".join(xml_strings)

        # Append beginning, end text.
        if self.rank == 0:
            xml_string = "<?xml version='1.0' encoding='utf-8'?>\n<materials>\n" + xml_string
        if self.rank == self.size:
            xml_string += "\n</materials>"

        xml_bytes = np.fromstring(xml_string, dtype=np.uint8)

        # Use MPI-IO to write to disk.
        # First, communicate to all nodes the length of their string.
        str_len = np.zeros(self.size, np.int32)

        str_my_len = np.zeros(1, np.int32)
        str_my_len[0] = len(xml_string)
        self.comm.Allgather([str_my_len, MPI.INT], [str_len, MPI.INT])

        # Compute index start.
        start_ind = np.sum(str_len[0:self.rank])

        # Open/create file
        handle = MPI.File.Open(self.comm, "materials.xml",
                               MPI.MODE_WRONLY | MPI.MODE_CREATE)

        handle.Seek(start_ind, MPI.SEEK_SET)
        handle.Write(xml_bytes)
        handle.Close()

        self.comm.barrier()

    def generate_settings_xml(self):
        """ Generates settings.xml.

        This function creates settings.xml using the value of the settings
        variable.

        Todo
        ----
            Rewrite to generalize source box.
        """

        if self.rank == 0:
            batches = self.settings.batches
            inactive = self.settings.inactive
            particles = self.settings.particles

            # Just a generic settings file to get it running.
            settings_file = openmc.Settings()
            settings_file.batches = batches
            settings_file.inactive = inactive
            settings_file.particles = particles
            settings_file.source = openmc.Source(space=openmc.stats.Box(
                self.settings.lower_left, self.settings.upper_right))

            if self.settings.entropy_dimension is not None:
                entropy_mesh = openmc.Mesh()
                entropy_mesh.lower_left = self.settings.lower_left
                entropy_mesh.upper_right = self.settings.upper_right
                entropy_mesh.dimension = self.settings.entropy_dimension
                settings_file.entropy_mesh = entropy_mesh

            # Set seed
            if self.settings.constant_seed is not None:
                seed = self.settings.constant_seed
            else:
                seed = random.randint(1, sys.maxsize-1)

            settings_file.seed = self.seed = seed

            settings_file.export_to_xml()

    def _get_tally_nuclides(self):
        nuc_set = set()

        # Create the set of all nuclides in the decay chain in cells marked for
        # burning in which the number density is greater than zero.
        for nuc in self.number.nuc_to_ind:
            if nuc in self.participating_nuclides:
                if np.sum(self.number[:, nuc]) > 0.0:
                    nuc_set.add(nuc)

        # Communicate which nuclides have nonzeros to rank 0
        if self.rank == 0:
            for i in range(1, self.size):
                nuc_newset = self.comm.recv(source=i, tag=i)
                nuc_set |= nuc_newset

            # Sort them in the same order as self.number
            nuc_list = []
            for nuc in self.number.nuc_to_ind:
                if nuc in nuc_set:
                    nuc_list.append(nuc)
        else:
            self.comm.send(nuc_set, dest=0, tag=self.rank)

        # Store list of tally nuclides on each process
        self.comm.bcast(nuc_list, root=0)
        tally_nuclides = []
        for nuc in nuc_list:
            if nuc in self.chain.nuclide_dict:
                tally_nuclides.append(nuc)

        return tally_nuclides

    def generate_tally_xml(self):
        """ Generates tally.xml.

        Using information from self.depletion_chain as well as the nuclides
        currently in the problem, this function automatically generates a
        tally.xml for the simulation.
        """
        tally_nuclides = self._get_tally_nuclides()

        if self.rank == 0:
            # Create tallies for depleting regions
            tally_ind = 1
            mat_filter_dep = openmc.MaterialFilter(
                [int(i) for i in self.mat_tally_ind], filter_id=1)
            tallies_file = openmc.Tallies()

            # For each reaction in the chain, for each nuclide, and for each
            # cell, make a tally
            tally_dep = openmc.Tally(tally_id=tally_ind)
            tally_dep.nuclides = tally_nuclides

            for reaction in self.chain.react_to_ind:
                tally_dep.scores.append(reaction)

            tallies_file.append(tally_dep)

            tally_dep.filters.append(mat_filter_dep)
            tallies_file.export_to_xml()

    def total_density_list(self):
        """ Returns a list of total density lists.

        This list is in the exact same order as depletion_matrix_list, so that
        matrix exponentiation can be done easily.

        Returns
        -------
        list of numpy.array
            A list of np.arrays containing total atoms of each cell.
        """

        total_density = [self.number.get_mat_slice(i) for i in range(self.number.n_mat_burn)]

        return total_density

    def set_density(self, total_density):
        """ Sets density.

        Sets the density in the exact same order as total_density_list outputs,
        allowing for internal consistency

        Parameters
        ----------
        total_density : list of numpy.array
            Total atoms.
        """

        # Fill in values
        for i in range(self.number.n_mat_burn):
            self.number.set_mat_slice(i, total_density[i])

    def unpack_tallies_and_normalize(self):
        """ Unpack tallies from OpenMC

        This function reads the tallies generated by OpenMC (from the tally.xml
        file generated in generate_tally_xml) normalizes them so that the total
        power generated is new_power, and then stores them in the reaction rate
        database.

        Returns
        -------
        k : float
            Eigenvalue of the last simulation.

        Todo
        ----
            Provide units for power
        """

        self.reaction_rates[:, :, :] = 0.0

        k_combined = openmc.capi.keff()[0]

        # Extract tally bins
        materials = list(self.mat_tally_ind.keys())
        nuclides = openmc.capi.tallies[1].nuclides
        reactions = list(self.chain.react_to_ind.keys())

        # Form fast map
        nuc_ind = [self.reaction_rates.nuc_to_ind[nuc] for nuc in nuclides]
        react_ind = [self.reaction_rates.react_to_ind[react] for react in reactions]

        # Compute fission power
        # TODO : improve this calculation

        power = 0.0

        power_vec = np.zeros(self.reaction_rates.n_nuc)

        fission_ind = self.reaction_rates.react_to_ind["fission"]

        for nuclide in self.chain.nuclides:
            if nuclide.name in self.reaction_rates.nuc_to_ind:
                for rx in nuclide.reactions:
                    if rx.type == 'fission':
                        ind = self.reaction_rates.nuc_to_ind[nuclide.name]
                        power_vec[ind] = rx.Q*1e-6
                        break

        # Extract results
        for i, mat in enumerate(self.number.burn_mat_list):
            # Get tally index
            slab = materials.index(mat)

            # Get material results hyperslab
            arr = openmc.capi.tallies[1].results
            results = arr[slab, :, 1]

            results_expanded = np.zeros((self.reaction_rates.n_nuc, self.reaction_rates.n_react))
            number = np.zeros(self.reaction_rates.n_nuc)

            # Expand into our memory layout
            j = 0
            for nuc, i_nuc_results in zip(nuclides, nuc_ind):
                number[i_nuc_results] = self.number[mat, nuc]
                for react in react_ind:
                    results_expanded[i_nuc_results, react] = results[j]
                    j += 1

            # Add power
            power += np.dot(results_expanded[:, fission_ind], power_vec)

            # Divide by total number and store
            for i_nuc_results in nuc_ind:
                if number[i_nuc_results] != 0.0:
                    for react in react_ind:
                        results_expanded[i_nuc_results, react] /= number[i_nuc_results]

            self.reaction_rates.rates[i, :, :] = results_expanded

        # Reduce power from all processes
        power = self.comm.allreduce(power)

        self.reaction_rates[:, :, :] *= (self.settings.power / power)

        return k_combined

    def load_participating(self):
        """ Loads a cross_sections.xml file to find participating nuclides.

        This allows for nuclides that are important in the decay chain but not
        important neutronically, or have no cross section data.
        """

        # Reads cross_sections.xml to create a dictionary containing
        # participating (burning and not just decaying) nuclides.

        try:
            filename = os.environ["OPENMC_CROSS_SECTIONS"]
        except KeyError:
            filename = None

        self.participating_nuclides = set()

        try:
            tree = ET.parse(filename)
        except:
            if filename is None:
                msg = "No cross_sections.xml specified in materials."
            else:
                msg = 'Cross section file "{}" is invalid.'.format(filename)
            raise IOError(msg)

        root = tree.getroot()
        self.burn_nuc_to_ind = OrderedDict()
        nuc_ind = 0

        for nuclide_node in root.findall('library'):
            mats = nuclide_node.get('materials')
            if not mats:
                continue
            for name in mats.split():
                # Make a burn list of the union of nuclides in cross_sections.xml
                # and nuclides in depletion chain.
                if name not in self.participating_nuclides:
                    self.participating_nuclides.add(name)
                    if name in self.chain.nuclide_dict:
                        self.burn_nuc_to_ind[name] = nuc_ind
                        nuc_ind += 1

    @property
    def n_nuc(self):
        """Number of nuclides considered in the decay chain."""
        return len(self.chain.nuclides)

    def get_results_info(self):
        """ Returns volume list, cell lists, and nuc lists.

        Returns
        -------
        volume : dict of str float
            Volumes corresponding to materials in full_burn_dict
        nuc_list : list of str
            A list of all nuclide names. Used for sorting the simulation.
        burn_list : list of int
            A list of all cell IDs to be burned.  Used for sorting the simulation.
        full_burn_dict : OrderedDict of str to int
            Maps cell name to index in global geometry.
        """

        nuc_list = self.number.burn_nuc_list
        burn_list = self.number.burn_mat_list

        volume = {}
        for i, mat in enumerate(burn_list):
            volume[mat] = self.number.volume[i]

        # Combine volume dictionaries across processes
        volume_list = self.comm.allgather(volume)
        volume = {k: v for d in volume_list for k, v in d.items()}

        return volume, nuc_list, burn_list, self.mat_tally_ind

def density_to_mat(dens_dict):
    """ Generates an OpenMC material from a cell ID and self.number_density.
    Parameters
    ----------
    m_id : int
        Cell ID.
    Returns
    -------
    openmc.Material
        The OpenMC material filled with nuclides.
    """

    mat = openmc.Material()
    for key in dens_dict:
        mat.add_nuclide(key, 1.0e-24*dens_dict[key])
    mat.set_density('sum')

    return mat

def clean_up_openmc():
    """ Resets all automatic indexing in OpenMC, as these get in the way. """
    openmc.reset_auto_ids()
