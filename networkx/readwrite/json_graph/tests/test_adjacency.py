import copy
import json

import pytest

import networkx as nx
from networkx.readwrite.json_graph import adjacency_data, adjacency_graph
from networkx.utils import graphs_equal


class TestAdjacency:


    def test_exception(self):
        with pytest.raises(nx.NetworkXError):
            G = nx.MultiDiGraph()
            attrs = {"id": "node", "key": "node"}
            adjacency_data(G, attrs)
