import pytest

import networkx as nx


class TestMinEdgeCover:
    """Tests for :func:`networkx.algorithms.min_edge_cover`"""

    def test_empty_graph(self):
        G = nx.Graph()
        assert nx.min_edge_cover(G) == set()

    def test_graph_with_loop(self):
        G = nx.Graph()
        G.add_edge(0, 0)
        assert nx.min_edge_cover(G) == {(0, 0)}

    def test_graph_with_isolated_v(self):
        G = nx.Graph()
        G.add_node(1)
        with pytest.raises(
            nx.NetworkXException,
            match="Graph has a node with no edge incident on it, so no edge cover exists.",
        ):
            nx.min_edge_cover(G)

    def test_graph_single_edge(self):
        G = nx.Graph([(0, 1)])
        assert nx.min_edge_cover(G) in ({(0, 1)}, {(1, 0)})


    def test_complete_graph_even(self):
        G = nx.complete_graph(10)
        min_cover = nx.min_edge_cover(G)
        assert nx.is_edge_cover(G, min_cover)
        assert len(min_cover) == 5


class TestIsEdgeCover:
    """Tests for :func:`networkx.algorithms.is_edge_cover`"""

    def test_empty_graph(self):
        G = nx.Graph()
        assert nx.is_edge_cover(G, set())

    def test_graph_with_loop(self):
        G = nx.Graph()
        G.add_edge(1, 1)
        assert nx.is_edge_cover(G, {(1, 1)})

    def test_graph_single_edge(self):
        G = nx.Graph()
        G.add_edge(0, 1)
        assert nx.is_edge_cover(G, {(0, 0), (1, 1)})
        assert nx.is_edge_cover(G, {(0, 1), (1, 0)})
        assert nx.is_edge_cover(G, {(0, 1)})
        assert not nx.is_edge_cover(G, {(0, 0)})
