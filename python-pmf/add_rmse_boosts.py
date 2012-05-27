#!/usr/bin/env python3

import argparse
from itertools import islice
import functools
import multiprocessing as mp
import os
import pickle

import numpy as np

from pmf_cy import ProbabilisticMatrixFactorization
from bayes_pmf import BayesianPMF



rmse = lambda exp, real: np.sqrt(((real - exp)**2).sum() / real.size)

def fit(real, known, latent_d=1, ret_pmf=False, subtract_mean=False,
        sig_u=1e10, sig_v=1e10, sig=1, 
        do_bayes=False, burnin=10, samps=200,
        stop_thresh=1e-10, min_learning_rate=1e-20):
    ratings = np.zeros((known.sum(), 3))
    for idx, (i, j) in enumerate(np.transpose(known.nonzero())):
        ratings[idx] = [i, j, real[i, j]]
    
    pmf = ProbabilisticMatrixFactorization(ratings, latent_d, subtract_mean)
    pmf.sigma_sq = sig
    pmf.sigma_u_sq = sig_u
    pmf.sigma_v_sq = sig_v
    pmf.stop_thresh = stop_thresh
    pmf.min_learning_rate = min_learning_rate
    pmf.fit()
    
    if not do_bayes:
        pred = pmf.predicted_matrix()
        return (pmf, pred) if ret_pmf else pred
    else:
        bpmf = BayesianPMF(ratings, 1)
        bpmf.__setstate__(pmf.__getstate__())
        sampler = bpmf.samples()
        
        # do burn-in
        next(islice(sampler, burnin, burnin), None)
        
        pred = bpmf.predict(islice(sampler, samps))
        return (bpmf, pred) if ret_pmf else pred


def fit_worker(real, known, num_fits, job_q, result_q, **fit_kwargs):
    real_rmse = functools.partial(rmse, real)

    for i, j in iter(job_q.get, None): # iterate until we see stop sentinel
        print(job_q.qsize())

        new_known = known.copy()
        new_known[i, j] = True

        fits = [fit(real=real, known=known, **fit_kwargs)
                for x in range(num_fits)]
        rmses = sorted(map(real_rmse, fits))
        result_q.put((i, j, fits, rmses), timeout=5)

    result_q.put(None) # send sentinel saying we're done


def get_fit_options(real, known, num_fits=3, pick=None, procs=None, **fit_kwargs):
    if procs is None:
        procs = mp.cpu_count()
    if pick is None:
        assert num_fits % 2 == 1
        pick = num_fits // 2
    real_rmse = functools.partial(rmse, real)
    
    pool = mp.Pool(min(procs, num_fits))
    print('Getting initial fits...')
    rs = [pool.apply_async(fit, (real, known), fit_kwargs)
            for x in range(num_fits)]
    pool.close()
    init_fits = [r.get() for r in rs]

    init_rmses = sorted(map(real_rmse, init_fits))
    init_rmse = init_rmses[pick]
    print('Initial RMSEs: ' + ', '.join("{:<5.4}".format(r) for r in init_rmses))
    pool.join()
    
    child_fits = {}
    child_rmses = {}
    rmses_arr = np.empty(real.shape); rmses_arr.fill(np.nan)

    job_q = mp.Queue()
    result_q = mp.Queue()

    workers = [mp.Process(target=fit_worker,
                          args=(real, known, num_fits, job_q, result_q),
                          kwargs=fit_kwargs)
               for _ in range(procs)]

    for w in workers:
        w.start()

    # put in actual jobs
    for i, j in zip(*np.logical_not(known).nonzero()):
        job_q.put((i, j))
    # sentinels to say you're done
    for w in workers:
        job_q.put(None)

    num_done = 0
    while True:
        resp = result_q.get()
        if resp is None:
            num_done += 1
            if num_done == procs:
                break
            continue

        i, j, fits, rmses = resp
        child_fits[i, j] = fits
        child_rmses[i, j] = rmses
        rmses_arr[i, j] = rmses[pick]

    for w in workers:
        w.join()

    return init_rmse, child_fits, child_rmses, rmses_arr



def add_to_datafile(path, force=False, num_fits=5, pick=None, procs=None,
                    sig_u=1e2, sig_v=1e2, latent_d=1,
                    stop_thresh=1e-10, min_learning_rate=1e-20):
    with open(path, 'rb') as f:
        data = pickle.load(f)

    if not force and '_rmse_boosts' in data:
        return

    real = data['_real']
    
    known = np.zeros(real.shape, bool)
    rated = data['_ratings'][:,:2].T.astype(int)
    known[tuple(rated)] = 1

    fit_args = dict(latent_d=latent_d, sig_u=sig_u, sig_v=sig_v,
            stop_thresh=stop_thresh, min_learning_rate=min_learning_rate)

    init, child_fits, child_rmses, rmses = get_fit_options(
            real, known, num_fits, pick, procs, **fit_args)

    data['_rmse_boosts'] = init - rmses
    data['_child_rmses'] = child_rmses

    with open(path + '.tmp', 'wb') as f:
        pickle.dump(data, f)
    os.rename(path, path + '.bak')
    os.rename(path + '.tmp', path)

def main():
    import argparse
    parser = argparse.ArgumentParser()

    parser.add_argument('--procs', '-P', type=int, default=None)
    parser.add_argument('--num-fits', '-n', type=int, default=5)
    parser.add_argument('--pick', type=int, default=None)

    parser.add_argument('--force', '-f', action='store_true', default=False)
    parser.add_argument('--no-force', action='store_false', dest='force')

    parser.add_argument('path')

    args = parser.parse_args()

    add_to_datafile(**vars(args))

if __name__ == '__main__':
    main()