import pytest

import networkx as nx


class TestAverageNeighbor:


    def test_degree_k4(self):
        G = nx.complete_graph(4)
        answer = {0: 3, 1: 3, 2: 3, 3: 3}
        nd = nx.average_neighbor_degree(G)
        assert nd == answer

        D = G.to_directed()
        nd = nx.average_neighbor_degree(D)
        assert nd == answer

        D = G.to_directed()
        nd = nx.average_neighbor_degree(D)
        assert nd == answer

        D = G.to_directed()
        nd = nx.average_neighbor_degree(D, source="in", target="in")
        assert nd == answer

    def test_degree_k4_nodes(self):
        G = nx.complete_graph(4)
        answer = {1: 3.0, 2: 3.0}
        nd = nx.average_neighbor_degree(G, nodes=[1, 2])
        assert nd == answer

    def test_degree_barrat(self):
        G = nx.star_graph(5)
        G.add_edges_from([(5, 6), (5, 7), (5, 8), (5, 9)])
        G[0][5]["weight"] = 5
        nd = nx.average_neighbor_degree(G)[5]
        assert nd == 1.8
        nd = nx.average_neighbor_degree(G, weight="weight")[5]
        assert nd == pytest.approx(3.222222, abs=1e-5)

    def test_error_invalid_source_target(self):
        G = nx.path_graph(4)
        with pytest.raises(nx.NetworkXError):
            nx.average_neighbor_degree(G, "error")
        with pytest.raises(nx.NetworkXError):
            nx.average_neighbor_degree(G, "in", "error")
        G = G.to_directed()
        with pytest.raises(nx.NetworkXError):
            nx.average_neighbor_degree(G, "error")
        with pytest.raises(nx.NetworkXError):
            nx.average_neighbor_degree(G, "in", "error")
