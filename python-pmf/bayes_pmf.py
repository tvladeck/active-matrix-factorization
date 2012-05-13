#!/usr/bin/env python3
'''
Implementation of Bayesian PMF (via Gibbs sampling).

Based on Matlab code by Ruslan Salakhutdinov:
http://www.mit.edu/~rsalakhu/BPMF.html
'''

from collections import defaultdict
from copy import deepcopy
from itertools import islice
import warnings

import numpy as np
from scipy import stats

try:
    from pmf_cy import ProbabilisticMatrixFactorization
except ImportError:
    warnings.warn("cython PMF not available; using pure-python version")
    from pmf import ProbabilisticMatrixFactorization

################################################################################
### Utilities

# This function by Matthew James Johnson, from:
# http://www.mit.edu/~mattjj/released-code/hsmm/stats_util.py
def sample_wishart(sigma, dof):
    '''
    Returns a sample from the Wishart distribution, the conjugate prior for
    precision matrices.
    '''
    n = sigma.shape[0]
    chol = np.linalg.cholesky(sigma)

    # use matlab's heuristic for choosing between the two different sampling
    # schemes
    if dof <= 81+n and dof == round(dof):
        # direct
        X = np.dot(chol, np.random.normal(size=(n,dof)))
    else:
        A = np.diag(np.sqrt(np.random.chisquare(dof - np.arange(0,n),size=n)))
        A[np.tri(n,k=-1,dtype=bool)] = np.random.normal(size=(n*(n-1)/2.))
        X = np.dot(chol,A)

    return np.dot(X, X.T)


def iter_mean(iterable):
    i = iter(iterable)
    total = next(i)
    count = -1
    for count, x in enumerate(i):
        total += x
    return total / (count + 2)

def copy_samples(samples_iter):
    return [deepcopy(sample) for sample in samples_iter]

################################################################################

class BayesianPMF(ProbabilisticMatrixFactorization):
    def __init__(self, rating_tuples, latent_d=5):
        super().__init__(rating_tuples, latent_d)

        self.beta = 2 # observation noise precision

        # parameters of inverse-wishart
        self.u_hyperparams = (
            np.eye(latent_d), # wi = wishart scale matrix (latent_d x latent_d)
            2, # b0 = scale on the gaussian's precision (scalar)
            latent_d, # degrees of freedom
            np.zeros(latent_d), # mu0 = mean of gaussian
        )

        self.v_hyperparams = (
            np.eye(latent_d), # wi = wishart scale matrix (latent_d x latent_d)
            2, # b0 = scale on the gaussian's precision (scalar)
            latent_d, # degrees of freedom
            np.zeros(latent_d), # mu0 = mean of gaussian
        )

    def __copy__(self):
        # need to copy fields from super
        res = BayesianPMF(self.ratings, self.latent_d)
        res.__setstate__(self.__getstate__())
        return res

    def __deepcopy__(self, memodict):
        # need to copy fields from super
        res = BayesianPMF(self.ratings, self.latent_d)
        res.__setstate__(deepcopy(self.__getstate__(), memodict))
        return res

    def __getstate__(self):
        state = super().__getstate__()
        #state.update(self.__dict__)
        state['__dict__'] = self.__dict__
        return state


    def sample_hyperparam(self, feats, do_users):
        '''
        Samples a mean hyperparameter conditional on the feature matrix
        (Gaussian-Wishart distribution).

        User hyperparams if do_users, otherwise item hyperparams.
        '''

        wi, b0, df, mu0 = self.u_hyperparams if do_users else self.v_hyperparams

        N = feats.shape[0]
        x_bar = np.mean(feats, axis=0).T
        S_bar = np.cov(feats, rowvar=0)

        mu0_xbar = mu0 - x_bar

        WI_post = np.linalg.inv(
                np.linalg.inv(wi)
                + N * S_bar
                + (b0 * N) / (b0 + N) * np.dot(mu0_xbar, mu0_xbar.T))
        WI_post /= 2
        WI_post = WI_post + WI_post.T

        alpha = sample_wishart(WI_post, df + N)

        mu_temp = (b0 * mu0 + N * x_bar) / (b0 + N)
        lam = np.linalg.cholesky(np.linalg.inv((b0 + N) * alpha))
        mu = np.dot(lam, np.random.normal(0, 1, self.latent_d)) + mu_temp

        return mu, alpha


    def sample_feature(self, n, is_user, mu, alpha, oth_feats,
                       rated_indices, ratings):
        '''
        Samples a user/item feature vector, conditional on the entire
        matrix of other item/user features.

        n: the id of the user/item
        is_user: true if this is a user, false if an item
        mu: the mean hyperparameter for users if is_user, items if not
        alpha: the precision hyperparamater
        oth_feats: self.items/self.users
        rated_indices: indices of the items rated by this user / users
                       who rated this item
        ratings: ratings by this user / for this item for rated_indices
        '''

        rated_feats = oth_feats[rated_indices, :]
        norm_ratings = ratings - self.mean_rating

        cov = np.linalg.inv(alpha +
                self.beta * np.dot(rated_feats.T, rated_feats))
        mean = np.dot(cov,
                self.beta * np.dot(rated_feats.T, norm_ratings)
                + np.dot(alpha, mu))

        lam = np.linalg.cholesky(cov)
        return np.dot(lam, np.random.normal(0, 1, self.latent_d)) + mean


    def samples(self, num_gibbs=2):
        '''
        Runs the Markov chain starting from the current MAP approximation in
        self.users, self.items. Yields sampled user, item features forever.

        Note that it actually just yields the same numpy arrays over and over:
        if you need access to the samples later, make a copy. That is, don't do
            samps = list(islice(self.samples(), n))
        Instead, do
            samps = copy_samples(islice(self.samples(), n))
        (using the copy_samples() utility function from above).

        If you add ratings after starting this iterator, it'll continue on
        without accounting for them.

        Does num_gibbs updates after each hyperparameter update, then yields
        the result.
        '''
        # find rated indices now, to avoid repeated lookups
        users_by_item = defaultdict(lambda: ([], []))
        items_by_user = defaultdict(lambda: ([], []))

        for user, item, rating in self.ratings:
            users_by_item[item][0].append(user)
            users_by_item[item][1].append(rating)

            items_by_user[user][0].append(item)
            items_by_user[user][1].append(rating)

        users_by_item = {k: (np.asarray(i, dtype=int), np.asarray(r))
                         for k, (i,r) in users_by_item.items()}
        items_by_user = {k: (np.asarray(i, dtype=int), np.asarray(r))
                         for k, (i,r) in items_by_user.items()}


        # initialize the Markov chain with the current MAP estimate
        # TODO: MAP search doesn't currently normalize by the mean rating
        #       should do that, or there'll be a while for burn-in to adjust
        user_sample = self.users.copy()
        item_sample = self.items.copy()

        # mu is the average value for each latent dimension
        mu_u = np.mean(user_sample, axis=0).T
        mu_v = np.mean(item_sample, axis=0).T

        # alpha is the inverse covariance among latent dimensions
        alpha_u = np.linalg.inv(np.cov(user_sample, rowvar=0))
        alpha_v = np.linalg.inv(np.cov(item_sample, rowvar=0))


        while True:
            # sample from hyperparameters
            mu_u, alpha_u = self.sample_hyperparam(user_sample, True)
            mu_v, alpha_v = self.sample_hyperparam(item_sample, False)

            # Gibbs updates for user, item feature vectors
            # TODO: parallelize

            for gibbs in range(num_gibbs):
                #print('\t\t Gibbs sampling {}'.format(gibbs))

                for user_id in range(self.num_users):
                    #print('user {}'.format(user_id))

                    user_sample[user_id, :] = self.sample_feature(
                            user_id, True, mu_v, alpha_v, item_sample,
                            *items_by_user[user_id])


                for item_id in range(self.num_items):
                    #print('item {}'.format(item_id))

                    item_sample[item_id, :] = self.sample_feature(
                            item_id, False, mu_v, alpha_v, user_sample,
                            *users_by_item[item_id])

            yield user_sample, item_sample

    def sample_pred(self, u, v):
        '''
        Gives the reconstruction based on a single u, v factorization.
        '''
        # TODO: cut off prediction to lie in valid range?
        return np.dot(u, v.T) + self.mean_rating

    def predict(self, samples_iter):
        '''
        Gives the mean reconstruction given a series of samples.
        '''
        return iter_mean(self.sample_pred(u, v) for u, v in samples_iter)

    def pred_variance(self, samples_iter):
        '''
        Gives the variance of each prediction in a series of samples.
        '''
        vals = [self.sample_pred(u, v) for u, v in samples_iter]
        return np.var(vals, 0)

    def prob_ge_cutoff(self, samples_iter, cutoff):
        '''
        Gives the portion of the time each matrix element was >= cutoff
        in a series of samples.
        '''
        counts = np.zeros((self.num_users, self.num_items))
        num = 0
        for u, v in samples_iter:
            counts += self.sample_pred(u, v) >= cutoff
            num += 1
        return counts / num

    def random(self, samples_iter):
        return np.random.rand(self.num_users, self.num_items)

    def bayes_rmse(self, samples_iter, true_r):
        pred = self.predict(samples_iter)
        return np.sqrt(((true_r - pred)**2).sum() / true_r.size)


################################################################################

def test_vs_map():
    from pmf import fake_ratings

    ratings, true_u, true_v = fake_ratings(noise=1)
    true_r = np.dot(true_u, true_v.T)

    ds = [3, 5, 8, 10, 12, 15]
    map_rmses = []
    bayes_rmses_1 = []
    bayes_rmses_2 = []
    bayes_rmses_combo = []

    for latent_d in ds:
        bpmf = BayesianPMF(ratings, latent_d)

        print("\ndimensionality: {}".format(latent_d))

        print("fitting MAP...")
        for ll in bpmf.fit_lls():
            pass
            #print("LL {}".format(ll))

        predicted_map = bpmf.predicted_matrix()

        print("doing MCMC...")
        samps = copy_samples(islice(bpmf.samples(), 500))

        bayes_rmses_1.append(bpmf.bayes_rmse(islice(samps, 250), true_r))
        bayes_rmses_2.append(bpmf.bayes_rmse(islice(samps, 250, None), true_r))
        bayes_rmses_combo.append(bpmf.bayes_rmse(samps, true_r))

        map_rmses.append(bpmf.rmse(true_r))

        print("MAP RMSE:               {}".format(map_rmses[-1]))
        print("Bayes RMSE [first 250]: {}".format(bayes_rmses_1[-1]))
        print("Bayes RMSE [next 250]:  {}".format(bayes_rmses_2[-1]))
        print("Bayes RMSE [combo]:     {}".format(bayes_rmses_combo[-1]))

    from matplotlib import pyplot as plt
    plt.plot(ds, map_rmses, label="MAP")
    plt.plot(ds, bayes_rmses_1, label="Bayes (first 250)")
    plt.plot(ds, bayes_rmses_2, label="Bayes (next 250)")
    plt.plot(ds, bayes_rmses_combo, label="Bayes (all 500)")
    plt.ylabel("RMSE")
    plt.xlabel("Dimensionality")
    plt.legend()
    plt.show()

################################################################################

KEYS = {
    'random': ("Random", 'random', True),
    'pred-variance': ("Pred Variance", 'pred_variance', True),

    'pred': ("Pred", 'predict', True),
    'prob-ge-3.5': ("Prob >= 3.5", 'prob_ge_cutoff', True, 3.5),
    'prob-ge-.5': ("Prob >= .5", 'prob_ge_cutoff', True, .5),
}

def full_test(bpmf, samples, real, key_fn, key_args, choose_max, num_samps):
    total = real.size
    picker_fn = getattr(bpmf, key_fn)

    yield (len(bpmf.rated), bpmf.bayes_rmse(samples, real), None, None)

    while bpmf.unrated:
        print("\nPicking query point...")

        if len(bpmf.unrated) == 1:
            vals = None
            i, j = next(iter(apmf.unrated))
        else:
            vals = picker_fn(samples, *key_args)
            idx = np.array(list(bpmf.unrated))
            test_vals = vals[idx[:,0], idx[:,1]]
            i, j = idx[np.argmax(test_vals), :]

        bpmf.add_rating(i, j, real[i, j])
        print("Queried (%d, %d); %d/%d known" % (i, j, len(bpmf.rated), total))

        print("Doing new MAP fit...")
        bpmf.fit()

        print("Getting new MCMC samples...")
        samples = copy_samples(islice(bpmf.samples(), num_samps))

        rmse = bpmf.bayes_rmse(samples, real)
        print("RMSE: {:.5}".format(rmse))
        yield len(bpmf.rated), rmse, (i,j), vals



def compare_active(key_names, latent_d, real, ratings, rating_vals=None,
                   num_samps=128, num_steps=None):
    # do initial fit
    bpmf_init = BayesianPMF(ratings, latent_d)
    print("Doing initial MAP fit...")
    bpmf_init.fit()

    print("Getting initial MCMC samples...")
    samples = copy_samples(islice(bpmf_init.samples(), num_samps))

    results = {
        '_real': real,
        '_ratings': ratings,
        '_rating_vals': rating_vals,
        '_initial_bpmf': deepcopy(bpmf_init),
    }

    # continue with each key for the fit
    for key_name in key_names:
        nice_name, key_fn, choose_max, *key_args = KEYS[key_name]
        print("\n\n" + "="*80 + "Testing {}".format(nice_name))

        res = full_test(deepcopy(bpmf_init), samples, real,
                        key_fn, key_args, choose_max, num_samps)
        results[key_name] = list(islice(res, num_steps))

    return results


def main():
    import argparse
    import os
    import pickle
    import sys

    key_names = KEYS.keys()

    # set up arguments
    parser = argparse.ArgumentParser()

    parser.add_argument('--latent-d', '-D', type=int, default=5)
    parser.add_argument('--steps', '-s', type=int, default=None)
    parser.add_argument('--samps', '-S', type=int, default=128)

    parser.add_argument('--load-data', required='True', metavar='FILE')
    parser.add_argument('--save-results', nargs='?', default=True, const=True,
            metavar='FILE')
    parser.add_argument('--no-save-results',
            action='store_false', dest='save_results')

    parser.add_argument('keys', nargs='*',
            help="Choices: {}.".format(', '.join(sorted(key_names))))

    args = parser.parse_args()

    # check that args.keys are valid
    for k in args.keys:
        if k not in key_names:
            sys.stderr.write("Invalid key name %s; options are %s.\n" % (
                k, ', '.join(sorted(key_names))))
            sys.exit(1)

    if not args.keys:
        args.keys = sorted(key_names)

    # make directories to save results if necessary
    if args.save_results is True:
        args.save_results = 'results.pkl'
    elif args.save_results:
        dirname = os.path.dirname(args.save_results)
        if dirname and not os.path.exists(dirname):
            os.makedirs(dirname)

    # load data
    with open(args.load_data, 'rb') as f:
        data = np.load(f)

        if isinstance(data, np.ndarray):
            data = { '_real': data }

        real = data['_real']
        ratings = data['_ratings']
        rating_vals = data['_rating_vals'] if '_rating_vals' in data else None

    # do the comparison
    try:
        results = compare_active(
                args.keys, args.latent_d,
                real, ratings, rating_vals,
                args.samps, args.steps)
    except Exception:
        import traceback
        print()
        traceback.print_exc()

        import pdb
        print()
        pdb.post_mortem()

        sys.exit(1)

    # save the results file
    if args.save_results:
        print("saving results in '{}'".format(args.save_results))

        results['_args'] = args

        with open(args.save_results, 'wb') as f:
            pickle.dump(results, f)

if __name__ == '__main__':
    main()
