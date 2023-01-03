# -*- coding: utf-8 -*-
#
# hl_api_sonata.py
#
# This file is part of NEST.
#
# Copyright (C) 2004 The NEST Initiative
#
# NEST is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 2 of the License, or
# (at your option) any later version.
#
# NEST is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with NEST.  If not, see <http://www.gnu.org/licenses/>.

"""
Class for building and simulating networks represented by the SONATA format
"""

import os
import json
import numpy as np
import pandas as pd
import itertools
from pathlib import Path, PurePath

from .. import pynestkernel as kernel
from ..ll_api import sps, sr, sli_func
from .hl_api_models import GetDefaults
from .hl_api_nodes import Create
from .hl_api_simulation import SetKernelStatus, Simulate
from .hl_api_types import NodeCollection

try:
    import h5py
    have_h5py = True
except ImportError:
    have_h5py = False

have_hdf5 = sli_func("statusdict/have_hdf5 ::")

__all__ = [
    "SonataNetwork"
]


class SonataNetwork():
    """Class for network models represented by the SONATA format.

    `SonataNetwork` provides native NEST support for building and simulating
    network models represented by the SONATA format. In the SONATA format,
    information about nodes, edges and their respective properties are stored
    in the table-based file formats HDF5 and CSV. Model metadata, such as path
    relation between files on disk and simulation parameters, are stored in
    JSON configuration files. See the NEST SONATA guide [LINK TO GUIDE] for
    details on the NEST support of the SONATA format.

    The constructor takes the JSON configuration file specifying the paths to
    the HDF5 and CSV files describing the network. In case simulation
    parameters are stored in a separate JSON configuration file, the
    constructor also has the option to pass a second configuration file.

    Parameters
    ----------
    config : str
        String describing the path to the JSON configuration file.
    sim_config : str, optional
        String describing the path to a JSON configuration file containing
        simulation parameters. This is only needed if simulation parameters
        are given in a separate configuration file.

    Example
    -------
        ::

            import nest

            nest.ResetKernel()

            # Specify path to the SONATA .json configuration file
            config = "path/to/config.json"

            # Instantiate SonataNetwork
            sonata_net = nest.SonataNetwork(config)

            # Create and connect nodes
            node_collections = sonata_net.BuildNetwork()

            # Connect spike recorder to a population
            s_rec = nest.Create("spike_recorder")
            pop_name = "name_of_population_to_record"
            nest.Connect(node_collections[pop_name], s_rec)

            # Simulate the network
            sonata_net.Simulate()
    """

    def __init__(self, config, sim_config=None):

        if not have_hdf5:
            msg = ("SonataNetwork unavailable because NEST was compiled "
                   "without HDF5 support")
            raise kernel.NESTError(msg)
        if not have_h5py:
            msg = ("SonataNetwork unavailable because h5py is not installed "
                   "or could not be imported")
            raise kernel.NESTError(msg)

        self._node_collections = {}
        self._edges_maps = []
        self._chunk_size_default = 2**20

        self._is_nodes_created = False
        self._is_network_built = False

        self._conf = self._parse_config(config)
        if sim_config is not None:
            self._conf.update(self._parse_config(sim_config))

        if self._conf["target_simulator"] != "NEST":
            msg = "'target_simulator' in configuration file must be 'NEST'."
            raise ValueError(msg)

    def _parse_config(self, config):
        """Parse JSON configuration file.

        Parse JSON config file and convert to a dictionary containing
        absolute paths and simulation parameters.

        Parameters
        ----------
        config : str
            String describing the path to the JSON config file.

        Returns
        -------
        dict
            SONATA config as dictionary
        """

        if not isinstance(config, str):
            msg = "Path to JSON configuration file must be passed as str"
            raise TypeError(msg)

        # Get absolute path
        conf_path = Path(config).resolve(strict=True)
        base_path = conf_path.parent

        with open(conf_path) as fp:
            conf = json.load(fp)

        # Replace path variables (e.g. $MY_DIR) with absolute paths in manifest
        for k, v in conf["manifest"].copy().items():
            if "$BASE_DIR" in v:
                v = v.replace("$BASE_DIR", ".")
            conf["manifest"].update({k: base_path.joinpath(v).as_posix()})

            if k.startswith("$"):
                conf["manifest"][k[1:]] = conf["manifest"].pop(k)

        def recursive_substitutions(config_obj):
            # Recursive substitutions of path variables with entries from manifest
            if isinstance(config_obj, dict):
                return {k: recursive_substitutions(v) for k, v in config_obj.items()}
            elif isinstance(config_obj, list):
                return [recursive_substitutions(e) for e in config_obj]
            elif isinstance(config_obj, str) and config_obj.startswith("$"):
                for dir, path in conf["manifest"].items():
                    config_obj = config_obj.replace(dir, path)
                return config_obj[1:]
            else:
                return config_obj

        conf.update(recursive_substitutions(conf))

        return conf

    def Create(self):
        """Create the SONATA network nodes.

        The network nodes are created on the Python level. In the SONATA format,
        node populations are serialized in node HD5 files. Each node in a
        population has a node type. Each node population has a single associated
        node types CSV file that assigns properties to all nodes with a given
        node type.

        Please note that it is assumed that all relevant node
        properties are stored in the node types CSV file. For neuron nodes
        relevant properties  are model type, model template and reference to a
        JSON file describing the parametrization.

        Returns
        -------
        node_collections : dict
            A dictionary containing the created NodeCollections [REF]. The
            population names are keys.
        """

        # Iterate node config files
        for nodes_conf in self._conf["networks"]["nodes"]:

            csv_fn = nodes_conf["node_types_file"]
            nodes_df = pd.read_csv(csv_fn, sep=r"\s+")

            # Require only one model type per CSV file
            model_types_arr = nodes_df["model_type"].to_numpy()
            is_one_model_type = (model_types_arr[0] == model_types_arr).all()

            if not is_one_model_type:
                msg = ("Only one model type per node CSV file is supported. "
                       f"{csv_fn} contains more than one.")
                raise ValueError(msg)

            model_type = model_types_arr[0]

            if model_type in ["point_neuron", "point_process"]:
                self._create_neurons(nodes_conf, nodes_df, csv_fn)
            elif model_type == "virtual":
                self._create_spike_generators(nodes_conf)
            else:
                msg = (f"Model type '{model_type}' in {csv_fn} is not "
                       "supported by NEST.")
                raise ValueError(msg)

        self._is_nodes_created = True

        return self._node_collections

    def _create_neurons(self, nodes_conf, nodes_df, csv_fn):
        """Create neuron nodes.

        Parameters
        ----------
        nodes_conf : dict
            Config as dictionary specifying filenames
        nodes_df : pandas.DataFrame
            Associated node CSV table as dataframe
        csv_fn : str
            Name of current CSV file. Used for more informative error messages.
        """

        node_types_map = self._create_node_type_parameter_map(nodes_df, csv_fn)

        models_arr = nodes_df["model_template"].to_numpy()
        is_one_model = (models_arr[0] == models_arr).all()
        one_model_name = models_arr[0] if is_one_model else None

        with h5py.File(nodes_conf["nodes_file"], "r") as nodes_h5f:

            # Iterate node populations in current node.h5 file
            for pop_name in nodes_h5f["nodes"]:

                node_type_id_dset = nodes_h5f["nodes"][pop_name]["node_type_id"][:]

                if is_one_model:
                    nest_nodes = Create(one_model_name,
                                        n=node_type_id_dset.size)
                    node_type_ids, inv_ind = np.unique(node_type_id_dset,
                                                       return_inverse=True)

                    # Extract node parameters
                    for i, node_type_id in enumerate(node_type_ids):
                        params_path = PurePath(self._conf["components"]["point_neuron_models_dir"],
                                               node_types_map[node_type_id]["dynamics_params"])

                        with open(params_path) as fp:
                            params = json.load(fp)

                        nest_nodes[inv_ind == i].set(params)
                else:
                    # More than one NEST neuron model in CSV file

                    # TODO: Utilizing np.unique(node_type_id_dset, return_<foo>=...)
                    # with the different return options might be more efficient

                    nest_nodes = NodeCollection()
                    for k, g in itertools.groupby(node_type_id_dset):
                        # k is a node_type_id key
                        # g is an itertools group object
                        # len(list(g)) gives the number of consecutive occurrences of the current k
                        model = node_types_map[k]["model_template"]
                        n_nrns = len(list(g))
                        params_path = PurePath(self._conf["components"]["point_neuron_models_dir"],
                                               node_types_map[k]["dynamics_params"])
                        with open(params_path) as fp:
                            params = json.load(fp)

                        nest_nodes += Create(model, n=n_nrns, params=params)

                self._node_collections[pop_name] = nest_nodes

    def _create_spike_generators(self, nodes_conf):
        """Create spike generator nodes.

        Parameters
        ----------
        nodes_conf : dict
            Config as dictionary specifying filenames
        """

        with h5py.File(nodes_conf["nodes_file"], "r") as nodes_h5f:
            for pop_name in nodes_h5f["nodes"]:

                node_type_id_dset = nodes_h5f["nodes"][pop_name]["node_type_id"]
                n_nodes = node_type_id_dset.size

                input_file = None
                for inputs_dict in self._conf["inputs"].values():
                    if inputs_dict["node_set"] == pop_name:
                        input_file = inputs_dict["input_file"]
                        break  # Break once we found the matching population

                if input_file is None:
                    msg = ("Could not find an input file for population "
                           f"{pop_name} in config file.")
                    raise ValueError(msg)

                with h5py.File(input_file, "r") as input_h5f:

                    # Deduce the HDF5 file structure
                    all_groups = all([isinstance(g, h5py.Group) for g in input_h5f["spikes"].values()])
                    any_groups = any([isinstance(g, h5py.Group) for g in input_h5f["spikes"].values()])
                    if (all_groups or any_groups) and not (all_groups and any_groups):
                        msg = ("Unsupported HDF5 structure; groups and "
                               "datasets cannot be on the same hierarchical "
                               f"level in input spikes file {input_file}")
                        raise ValueError(msg)

                    if all_groups:
                        if pop_name in input_h5f["spikes"].keys():
                            spikes_grp = input_h5f["spikes"][pop_name]
                        else:
                            msg = ("Did not find a matching HDF5 group name "
                                   f"for population {pop_name} in {input_file}")
                            raise ValueError(msg)
                    else:
                        spikes_grp = input_h5f["spikes"]

                    if "gids" in spikes_grp:
                        node_ids = spikes_grp["gids"][:]
                    elif "node_ids" in spikes_grp:
                        node_ids = spikes_grp["node_ids"][:]
                    else:
                        msg = ("No dataset called 'gids' or 'node_ids' in "
                               f"{input_file}")
                        raise ValueError(msg)

                    timestamps = spikes_grp["timestamps"][:]

                # Map node id's to spike times
                # TODO: Can this be done in a more efficient way?
                spikes_map = {node_id: timestamps[node_ids == node_id] for node_id in range(n_nodes)}
                params_lst = [{"spike_times": spikes_map[node_id], "precise_times": True} for node_id in range(n_nodes)]

                # Create and store NC
                nest_nodes = Create("spike_generator", n=n_nodes, params=params_lst)
                self._node_collections[pop_name] = nest_nodes

    def _create_node_type_parameter_map(self, nodes_df, csv_fn):
        """Create map between node type id and node properties.

        For neuron models, each node type id in the node types CSV file has:
            * A model template which describes the name of the neuron model
            * A reference to a JSON file describing the neuron's parametrization

        This function creates a map of the above node properties with the
        node type id as key.

        Parameters
        ----------
        nodes_df : pandas.DataFrame
            Node type CSV table as dataframe
        csv_fn : str
            Name of current CSV file. Used for more informative error messages.

        Returns
        -------
        dict :
            Map of node properties for the different node type ids
        """

        if "model_template" not in nodes_df.columns:
            msg = ("Missing the required 'model_template' header specifying "
                   f"NEST neuron models in {csv_fn}.")
            raise ValueError(msg)

        if "dynamics_params" not in nodes_df.columns:
            msg = ("Missing the required 'dynamics_params' header specifying "
                   f".json files with model parameters in {csv_fn}")
            raise ValueError(msg)

        nodes_df["model_template"] = nodes_df["model_template"].str.replace("nest:", '')

        req_cols = ["model_template", "dynamics_params"]
        node_types_map = nodes_df.set_index("node_type_id")[req_cols].to_dict(orient="index")

        return node_types_map

    def Connect(self, chunk_size=None):
        """Connect the SONATA network nodes.

        The connections are created by first parsing the edge (synapse) CSV
        files to create a map of synaptic properties on the Python level. This
        is then sent to the NEST kernel together with the edge HDF5 files to
        create the connections.

        For large networks, the edge HDF5 files might not fit into memory in
        their entirety. In the NEST kernel, the edge HDF5 datasets are therefore
        read sequentially in chunks. The chunk size is modifiable so that the
        user is able to achieve a balance between the number of read operations
        and memory overhead.

        Parameters
        ----------
        chunk_size : int, optional
            Size of the chunk to read in one read operation. The chunk size
            is applied to all HDF5 datasets that need to be read in order to
            create the connections. Default: `2**20`.
        """

        if not self._is_nodes_created:
            msg = ("The SONATA network nodes must be created before any "
                   "connections can be made")
            raise kernel.NESTError(msg)

        if chunk_size is None:
            chunk_size = self._chunk_size_default

        self._verify_chunk_size(chunk_size)
        self._create_edges_maps()

        graph_specs = {"nodes": self._node_collections,
                       "edges": self._edges_maps}

        # Check if HDF5 files exist and are not blocked.
        for d in self._edges_maps:
            try:
                f = h5py.File(d["edges_file"], "r")
                f.close()
            except BlockingIOError as err:
                raise BlockingIOError(f"{err.strerror} for {os.path.realpath(d['edges_file'])}") from None

        sps(graph_specs)
        sps(chunk_size)
        sr("Connect_sonata")

        self._is_network_built = True

    def _verify_chunk_size(self, chunk_size):
        """Check if provided chunk size is valid."""

        if not isinstance(chunk_size, int):
            raise TypeError("chunk_size must be passed as int")
        if chunk_size <= 0:
            raise ValueError("chunk_size must be strictly positive")

    def _create_edges_maps(self):
        """Create a collection of maps of edge properties.

        Creates a map between edge type id and edge (synapse) properties for
        each edge CSV file. The associated edge HDF5 filename is included in
        the map as well.
        """

        # Iterate edge config files
        for edges_conf in self._conf["networks"]["edges"]:

            edges_map = {}
            edges_csv_fn = edges_conf["edge_types_file"]
            edges_df = pd.read_csv(edges_csv_fn, sep=r"\s+")

            if "model_template" not in edges_df.columns:
                msg = ("Missing the required 'model_template' header "
                       f"specifying NEST synapse models in {edges_csv_fn}.")
                raise ValueError(msg)

            # Rename column labels to names used by NEST. Note that rename
            # don't throw an error for extra labels (we want this behavior)
            edges_df.rename(columns={"model_template": "synapse_model",
                                     "syn_weight": "weight"},
                            inplace=True)

            models_arr = edges_df["synapse_model"].to_numpy()
            is_one_model = (models_arr[0] == models_arr).all()
            have_dynamics = True if "dynamics_params" in edges_df.columns else False
            edges_df_cols = set(edges_df.columns)

            if is_one_model:
                synapse_model = models_arr[0]
                # Set of settable parameters
                settable_params = set([*GetDefaults(synapse_model)])
                # Parameters to extract (elements common to both sets)
                extract_cols = list(settable_params & edges_df_cols)
                if have_dynamics:
                    extract_cols.append("dynamics_params")

                syn_specs = edges_df.set_index("edge_type_id")[extract_cols].to_dict(orient="index")

                if have_dynamics:
                    # Include parameters from JSON file in the map
                    for edge_type_id, syn_spec in syn_specs.copy().items():

                        params_path = PurePath(self._conf["components"]["synaptic_models_dir"],
                                               syn_spec["dynamics_params"])
                        with open(params_path) as fp:
                            params = json.load(fp)

                        syn_specs[edge_type_id].update(params)
                        syn_specs[edge_type_id].pop("dynamics_params")
            else:
                # More than one synapse model in CSV file
                syn_specs = {}
                idx_map = {k: i for i, k in enumerate(list(edges_df), start=1)}

                for row in edges_df.itertuples(name=None):
                    # Set of settable parameters
                    settable_params = set([*GetDefaults(row[idx_map["synapse_model"]])])
                    # Parameters to extract (elements common to both sets)
                    extract_cols = list(settable_params & edges_df_cols)
                    syn_spec = {k: row[idx_map[k]] for k in extract_cols}

                    if have_dynamics:
                        # Include parameters from JSON file in the map
                        params_path = PurePath(self._conf["components"]["synaptic_models_dir"],
                                               row[idx_map["dynamics_params"]])

                        with open(params_path) as fp:
                            params = json.load(fp)

                        syn_spec.update(params)

                    syn_specs[row[idx_map["edge_type_id"]]] = syn_spec

            # Create edges map
            edges_map["syn_specs"] = syn_specs
            edges_map["edges_file"] = edges_conf["edges_file"]
            self._edges_maps.append(edges_map)

    def BuildNetwork(self, chunk_size=None):
        """Build SONATA network.

        Convenience function for building the SONATA network. The function
        first calls `Create` [REF] to create the network nodes and then
        `Connect` [REF] to create their connections. For more details, see
        Create [REF] and Connect [REF].

        Parameters
        ----------
        chunk_size : int, optional
            Size of chunks from all relevant edge HDF5 datasets that are
            read into memory at once in order to create connections.
            Default: `2**20`.

        Returns
        -------
        node_collections : dict
            A dictionary containing the created NodeCollections [REF]. The
            population names are keys.
        """

        if chunk_size is not None:
            # Chunk size is verfified in Connect, but we also verify here
            # to save computational resources in case of wrong input
            self._verify_chunk_size(chunk_size)

        node_collections = self.Create()
        self.Connect(chunk_size=chunk_size)

        return node_collections

    def Simulate(self, **kwargs):
        """Simulate the SONATA network.

        The simulation time and resolution is expected to be provided in the
        JSON configuration file. Additional kernel attributes can be passed as
        as arbitrary keyword arguments (`kwargs`). See the documentation of
        :ref:`sec:kernel_attributes` for a valid list of kernel attributes.

        Note that the number of threads and MPI processes should be set in
        advance of *building* the network.

        Parameters
        ----------
        kwargs:
            kwargs are passed to SetKernelStatus [REF].
        """

        # Verify that network is built
        if not self._is_network_built:
            msg = ("The SONATA network must be built before a simulation "
                   "can be done")
            raise kernel.NESTError(msg)

        SetKernelStatus(**kwargs)

        if not "dt" in self._conf["run"]:
            msg = ("Time resolution 'dt' must be specified in configuration "
                   "file")
            raise ValueError(msg)

        SetKernelStatus({"resolution": self._conf["run"]["dt"]})

        if "tstop" in self._conf["run"]:
            T_sim = self._conf["run"]["tstop"]
        elif "duration" in self._conf["run"]:
            T_sim = self._conf["run"]["duration"]
        else:
            msg = ("Simulation time 'tstop' or 'duration' must be specified "
                   "in configuration file")
            raise ValueError(msg)

        Simulate(T_sim)

    @ property
    def node_collections(self):
        return self._node_collections

    @ property
    def config(self):
        return self._conf
