from sklearn.neighbors import NearestNeighbors
import numpy as np


class Matcher:
    def __init__(self, embeddings: np.ndarray):
        self.embeddings = embeddings
        self.nn = NearestNeighbors(n_neighbors=5, algorithm='auto')
        self.nn.fit(embeddings)

    def query(self, embedding: np.ndarray, top_k: int = 5):
        dists, idxs = self.nn.kneighbors(embedding.reshape(1, -1), n_neighbors=top_k)
        return idxs[0], dists[0]
