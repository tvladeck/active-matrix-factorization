#!/usr/bin/env python3
'''
An implementation of Probabilistic Matrix Factorization:
    Salakhutdinov, R., & Mnih, A. (2007). Probabilistic Matrix Factorization.
    Advances in Neural Information Processing Systems.

Loosely based on code by Danny Tarlow:
http://blog.smellthedata.com/2009/06/netflix-prize-tribute-recommendation.html
'''

from __future__ import print_function # silly cython

import itertools
import random
from copy import deepcopy

import numpy as np
cimport numpy as np

cimport cython

DTYPE = np.float
ctypedef np.float_t DTYPE_t

cdef class ProbabilisticMatrixFactorization:
    cdef public int latent_d, num_users, num_items
    cdef public float learning_rate, min_learning_rate, stop_thresh
    cdef public float sigma_sq, sigma_u_sq, sigma_v_sq
    cdef public np.ndarray ratings, users, items
    cdef public object rated, unrated

    def __cinit__(self):
        self.learning_rate = 1e-4
        self.min_learning_rate = 1e-10
        self.stop_thresh = 1e-2

        self.sigma_sq = 1
        self.sigma_u_sq = 10
        self.sigma_v_sq = 10

    def __init__(self, np.ndarray rating_tuples not None, int latent_d=1):
        self.latent_d = latent_d

        self.ratings = np.array(rating_tuples, dtype=float, copy=False)
        if self.ratings.shape[1] != 3:
            raise TypeError("invalid rating tuple length")

        cdef int n, m
        self.num_users = n = int(np.max(self.ratings[:, 0]) + 1)
        self.num_items = m = int(np.max(self.ratings[:, 1]) + 1)

        self.rated = set(map(tuple, self.ratings[:,:2]))
        self.unrated = set(itertools.product(range(n), range(m)))\
                .difference(self.rated)

        self.users = np.random.random((n, self.latent_d))
        self.items = np.random.random((m, self.latent_d))


    cpdef ProbabilisticMatrixFactorization __copy__(self):
        cdef ProbabilisticMatrixFactorization res
        res = type(self)(self.ratings, self.latent_d)
        res.__setstate__(res.__getstate__())
        return res

    cpdef ProbabilisticMatrixFactorization __deepcopy__(self, memodict):
        cdef ProbabilisticMatrixFactorization res
        res = type(self)(self.ratings.copy(), self.latent_d)
        res.__setstate__(deepcopy(res.__getstate__(), memodict))
        return res

    def __setstate__(self, state):
        for k, v in state.items():
            setattr(self, k, v)

    def __getstate__(self):
        return dict(
            latent_d=self.latent_d,
            num_users=self.num_users,
            num_items=self.num_items,

            learning_rate=self.learning_rate,
            min_learning_rate=self.min_learning_rate,
            stop_thresh=self.stop_thresh,

            sigma_sq=self.sigma_sq,
            sigma_u_sq=self.sigma_u_sq,
            sigma_v_sq=self.sigma_v_sq,

            ratings=self.ratings,
            users=self.users,
            items=self.items,

            rated=self.rated,
            unrated=self.unrated,
        )

    def add_rating(self, int i, int j, DTYPE_t rating):
        self.add_ratings([i, j, rating])

    def add_ratings(self, extra):
        cdef int rows = self.ratings.shape[0]
        cdef int cols = self.ratings.shape[1]

        extra = np.array(extra, copy=False, ndmin=2)
        if len(extra.shape) != 2 or extra.shape[1] != cols:
            raise TypeError("bad shape for extra")

        assert np.max(extra[:,0] + 1) <= self.num_users
        assert np.max(extra[:,1] + 1) <= self.num_items

        new_items = set((int(i), int(j)) for i, j in extra[:,:2])

        if not new_items.issubset(self.unrated):
            raise ValueError("can't rate already rated items")
        self.rated.update(new_items)
        self.unrated.difference_update(new_items)

        self.ratings = np.append(self.ratings, extra, 0)
        # TODO: this can be done faster with .resize()...
        #       but the commented-out version is way broken
        # new_rows = extra.shape[0]
        # try:
        #     self.ratings.resize(rows + new_rows, cols)
        # except ValueError:
        #     self.ratings = self.ratings.copy()
        #     self.ratings.resize(rows + new_rows, cols)
        # self.ratings[rows:, :] = extra


    @cython.cdivision(True)
    cpdef float log_likelihood(self, users=None, items=None) except? 1492:
        if users is None:
            users = self.users
        if items is None:
            items = self.items

        cdef int i, j
        cdef float rating, r_hat, sq_error

        sq_error = 0
        for i, j, rating in self.ratings:
            r_hat = np.dot(users[i,:], items[j,:])
            sq_error += (rating - r_hat)**2

        cdef float user_norm = np.linalg.norm(users)
        cdef float item_norm = np.linalg.norm(users)

        return (- sq_error / (2 * self.sigma_sq)
                - user_norm / (2 * self.sigma_u_sq)
                - item_norm / (2 * self.sigma_v_sq))


    @cython.cdivision(True)
    cpdef object gradient(self):
        cdef np.ndarray[DTYPE_t,ndim=2] grad_u = -self.users / self.sigma_u_sq
        cdef np.ndarray[DTYPE_t,ndim=2] grad_v = -self.items / self.sigma_v_sq

        cdef float sig = self.sigma_sq
        cdef int i, j
        cdef float rating, r_hat

        for i, j, rating in self.ratings:
            r_hat = np.dot(self.users[i], self.items[j])
            grad_u[i, :] += self.items[j, :] * ((rating - r_hat) / sig)
            grad_v[j, :] += self.users[i, :] * ((rating - r_hat) / sig)

        return grad_u, grad_v


    def fit_lls(self):
        cdef np.ndarray grad_u, grad_v, new_users, new_items
        cdef float lr = self.learning_rate

        cdef float old_ll = self.log_likelihood()
        cdef float new_ll

        cdef bint converged = False
        while not converged:
            grad_u, grad_v = self.gradient()

            # take one step, trying different learning rates if necessary
            while True:
                #print "  setting learning rate =", lr
                new_users = self.users + lr * grad_u
                new_items = self.items + lr * grad_v
                new_ll = self.log_likelihood(new_users, new_items)

                if new_ll > old_ll:
                    self.users = new_users
                    self.items = new_items
                    lr *= 1.25

                    if new_ll - old_ll < self.stop_thresh:
                        converged = True
                    break
                else:
                    lr *= .5

                    if lr < self.min_learning_rate:
                        converged = True
                        break

            yield new_ll
            old_ll = new_ll


    def fit(self):
        for ll in self.fit_lls():
            pass

    cpdef np.ndarray predicted_matrix(self):
        return np.dot(self.users, self.items.T)

    cpdef float rmse(self, np.ndarray real) except -1:
        return np.sqrt(((real - self.predicted_matrix())**2).sum() / real.size)

    def print_latent_vectors(self):
        cdef int i, j
        print("Users:")
        for i in range(self.num_users):
            print("%d: %s" % (i, self.users[i, :]))

        print("\nItems:")
        for j in range(self.num_items):
            print("%d: %s" % (j, self.items[j, :]))


    def save_latent_vectors(self, prefix):
        self.users.dump(prefix + "%sd_users.pickle" % self.latent_d)
        self.items.dump(prefix + "%sd_items.pickle" % self.latent_d)


################################################################################
### Testing code

def fake_ratings(noise=.25, num_users=100, num_items=100, num_ratings=30,
                 latent_dimension=10):
    # Generate the latent user and item vectors
    u = np.random.normal(0, 2, (num_users, latent_dimension))
    v = np.random.normal(0, 2, (num_items, latent_dimension))

    # Get num_ratings ratings per user.
    ratings = []
    for i in range(num_users):
        for j in random.sample(range(num_items), num_ratings):
            rating = np.dot(u[i], v[j]) + np.random.normal(scale=noise)
            ratings.append((i, j, rating))

    return (np.array(ratings), u, v)


def plot_ratings(ratings):
    import matplotlib.pyplot as plt
    plt.plot(ratings[:, 1], ratings[:, 2], 'bx')
    plt.show()


def plot_latent_vectors(U, V):
    import matplotlib.pyplot as plt
    import matplotlib.cm as cm

    fig = plt.figure()
    ax = fig.add_subplot(121)
    cmap = cm.jet
    ax.imshow(U, cmap=cmap, interpolation='nearest')
    plt.title("Users")
    plt.axis("off")

    ax = fig.add_subplot(122)
    ax.imshow(V, cmap=cmap, interpolation='nearest')
    plt.title("Items")
    plt.axis("off")


def plot_predicted_ratings(U, V):
    import matplotlib.pyplot as plt
    from matplotlib import cm

    u_rows, u_cols = U.shape
    v_rows, v_cols = V.shape

    r_hats = -5 * np.ones((u_rows + u_cols + 1, v_rows + v_cols + 1))

    for i in range(u_rows):
        for j in range(u_cols):
            r_hats[i + v_cols + 1, j] = U[i, j]

    for i in range(v_rows):
        for j in range(v_cols):
            r_hats[j, i + u_cols + 1] = V[i, j]

    for i in range(u_rows):
        for j in range(v_rows):
            r_hats[i + u_cols + 1, j + v_cols + 1] = np.dot(U[i], V[j]) / 10

    fig = plt.figure()
    ax = fig.add_subplot(111)
    ax.imshow(r_hats, cmap=cm.gray, interpolation='nearest')
    plt.title("Predicted Ratings")
    plt.axis("off")


def main():
    import matplotlib.pyplot as plt

    (ratings, true_o, true_d) = fake_ratings()
    #plot_ratings(ratings)

    pmf = ProbabilisticMatrixFactorization(ratings, latent_d=5)

    lls = []
    for ll in pmf.fit_lls():
        lls.append(ll)
        print("LL =", ll)

    plt.figure()
    plt.plot(lls)
    plt.xlabel("Iteration")
    plt.ylabel("Log Likelihood")

    #plot_latent_vectors(pmf.users, pmf.items)
    #plot_predicted_ratings(pmf.users, pmf.items)
    #plt.show()

    #pmf.print_latent_vectors()
    #pmf.save_latent_vectors("models/")

if __name__ == '__main__':
    main()