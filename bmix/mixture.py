from collections import defaultdict
from functools import reduce
from itertools import permutations
from matplotlib import pyplot
from pyro import poutine
from pyro.distributions import *
from pyro.infer import Predictive, SVI, Trace_ELBO
from pyro.infer import SVI, TraceEnum_ELBO, TraceMeanField_ELBO, config_enumerate, infer_discrete
from pyro.infer.autoguide import AutoDelta, AutoNormal
from pyro.infer.mcmc import NUTS
from pyro.infer.mcmc.api import MCMC
from pyro.optim import Adam
from scipy.interpolate import CloughTocher2DInterpolator, NearestNDInterpolator
from sklearn.utils import resample
from torch.distributions import constraints
from tqdm import tqdm
import matplotlib as mpl
import matplotlib.pyplot as plt
import numpy as np
import os
import os.path as osp
import pandas as pd
import pyro
import pyro.distributions as dist
import pysptools.abundance_maps as amp
import pysptools.eea as eea
import pysptools.util as util
import scipy.stats
import torch
import torch.nn.functional as F

smoke_test = ('CI' in os.environ)
assert pyro.__version__.startswith('1.8.0')

pyro.set_rng_seed(3)
ndim = 2
K = T = 3
num_samples = 50
N = 500
alpha = 1
n_iter = 1600
optim = Adam({'lr': 0.005})
elbo = Trace_ELBO()

# This is the 'full' generative model
def model(data = None, scale = .01, alpha = alpha, covariance = True, alpha_components = 1,
         N = N):
    """
    alpha_components: dirichlet parameter for phase weights
    alpha: dirichlet parameter for phase mixing
    """
    # Global variables.
    weights = pyro.sample('weights', dist.Dirichlet(alpha_components * torch.ones(K)))

    concentration = torch.ones(
            ()
        )

    with pyro.plate('dims', ndim):
        scales = pyro.sample('scales', dist.Uniform(scale * .5, scale * 1.5))#dist.LogNormal(-2.5, 1))

    with pyro.plate('components', K):
        locs = pyro.sample('locs', dist.MultivariateNormal(torch.zeros(ndim), torch.eye(ndim)))

        if covariance:
            # Implies a uniform distribution over correlation matrices
            L_omega = pyro.sample("L_omega", LKJCholesky(ndim, concentration))

    with pyro.plate('data', N):
        # Local variables.
        local_weights = pyro.sample("phase_weights", Dirichlet(weights * alpha))

        weighted_expectation = pyro.deterministic('weighted_expectation',
                                    torch.einsum('...ji,...j->...i', locs, local_weights)
                                         )

        if covariance:
            # Lower cholesky factor of the covariance matrix
            L_Omega = pyro.deterministic('L_Omega',
                torch.matmul(torch.diag(scales.sqrt()),
                                   torch.einsum('...jik,...j->...ik', L_omega, local_weights))
            )

            pyro.sample('obs',
                        dist.MultivariateNormal(weighted_expectation, scale_tril=L_Omega),
                        obs=data)
        else:
            pyro.sample('obs',
                dist.MultivariateNormal(weighted_expectation, torch.eye(ndim) * scales),
                obs=data)

# In general we might use different models for simulation and inference
cfg = dict()
cfg['generation_model'] = model
cfg['inference_model'] = model


def dummy_model(data = None, scale = .01, alpha = alpha, covariance = True, alpha_components = 1,
         N = N):
    """
    dummy model for purposes of rendering a plate diagram.
    """
    # Global variables.
    weights = pyro.sample('global_weights', dist.Dirichlet(alpha_components * torch.ones(K)))

    concentration = torch.ones(
            ()
        )
    with pyro.plate('D', ndim):
        scales = pyro.sample('scale', dist.Uniform(scale * .5, scale * 1.5))#dist.LogNormal(-2.5, 1))
    with pyro.plate('K', K):
        locs = pyro.sample('locs', dist.MultivariateNormal(torch.zeros(ndim), torch.eye(ndim)))
        if covariance:
            # Implies a uniform distribution over correlation matrices
            L_omega = LKJCholesky(ndim, concentration).sample([K])
    with pyro.plate('N', N):
        # Local variables.
        local_weights = pyro.sample("phaseweight", Dirichlet(weights * alpha))
        weighted_expectation = torch.einsum('...ji,...j->...i', locs, local_weights)

        if covariance:
            # Lower cholesky factor of the covariance matrix
            L_Omega = torch.matmul(torch.diag(scales.sqrt()),
                                   torch.einsum('...jik,...j->...ik', L_omega, local_weights))
            pyro.sample('obs',
                        dist.MultivariateNormal(weighted_expectation, scale_tril=L_Omega),
                        obs=data)
        else:
            pyro.sample('obs',
                dist.MultivariateNormal(weighted_expectation, torch.eye(ndim) * scales),
                obs=data)

def log_likelihood_from_params(paramdict, data):
    return (
            dist.MultivariateNormal(paramdict['weighted_expectation'], scale_tril=paramdict['L_Omega'])
            .log_prob(data).sum().item()
        )

def get_log_likelihood(model, guide, data, num_samples = 50, posterior = None):
    if posterior is None:
        posterior = Predictive(model, guide = guide, num_samples=num_samples)()
    print(data.shape, posterior['weighted_expectation'].shape, posterior['L_Omega'].shape)
    return log_likelihood_from_params(posterior, data)

def gen_data(N = N, alpha = alpha, noise_scale = .01, alpha_components = 5):
    def _model(*args, **kwargs):
        return cfg['generation_model'](data = None, scale = noise_scale, alpha = alpha, alpha_components =\
                    alpha_components, N = N)

    prior_samples = Predictive(_model, {}, num_samples=1)()
    # TODO what's the difference between Predictive(model) and
    # Predictive(model, guide = guide)?
    return prior_samples['weighted_expectation'][0],\
        prior_samples['locs'][0], prior_samples['obs'][0], prior_samples['weights'][0], prior_samples


def vi_inference(data, num_samples, N, alpha, n_iter = n_iter, noise_scale = .01,
                n_likelihood_samples = 100, seed = None,
                guide_f = AutoNormal):
    res = dict()
    losses = []
    log_likelihoods = []
    def f(*args):
        return cfg['inference_model'](*args, scale = noise_scale, alpha = alpha, N = N)

#     pyro.clear_param_store()

    def train(num_iterations):
        pyro.clear_param_store()
#         if seed is not None:
#             pyro.set_rng_seed(seed)
        for j in tqdm(range(num_iterations)):
            if j % 100 == 0:
                ll = get_log_likelihood(f, guide, data, n_likelihood_samples)
                print('ll', ll)
                log_likelihoods.append(ll)
            loss = svi.step(data)
            losses.append(loss)

    guide = guide_f(f)

    svi = SVI(f, guide,
              optim=optim,
              loss=elbo)

    train(n_iter)

    res = {'predictive': Predictive(f, guide = guide, num_samples=num_samples)(),
          'model': f,
           'guide': guide,
          'losses': losses,
          'log_likelihoods': log_likelihoods}
    # TODO any other attributes that require this?
    res['predictive']['weights'] = res['predictive']['weights'].squeeze()
    return res

def rms(arr):
    arr = np.array(arr)
    return np.sqrt((arr**2).sum() / len(arr))

def loc_stds(component_locs):
    return np.sqrt((component_locs.std(axis = 0)**2).sum() / component_locs.shape[1])

def loc_means(component_locs):
    return component_locs.mean(axis = 0)

def closest_permutation(locs_posterior_means, locs):
    # Figure out mapping from ground truth end members to inferred end members
    clust_permutations = list(permutations(np.arange(T), r = T))
    permutation_norms = [np.linalg.norm(locs_posterior_means[ci_permute, :] - np.array(locs))
                         for ci_permute in clust_permutations]
    best_permutation = clust_permutations[np.argmin(permutation_norms)]
    return best_permutation

def closest_permutation_diffs(locs_posterior_means, locs):
    best_permutation = closest_permutation(locs_posterior_means, locs)
    locs_diffs = locs_posterior_means[best_permutation, :] - np.array(locs)
    return locs_diffs

def get_beta(va, vb, vc, norm = True):
    # area of the simplex drawn by the endmember coordinates, relative to that of the regular simplex
    vmean = reduce(lambda a, b: a + np.linalg.norm(b),
                   [vb - va, vc - vb, va - vc], 0) / 3
    ref_area = np.sqrt(3) * vmean**2 / 4 # area of equal-surface regular simplex
    beta = np.abs(np.cross((vb - va), (vc - va)))
    if norm:
        beta /= (2 * ref_area)
    return beta

def mcmc_posterior(data, num_samples, N, alpha, noise_scale = .01, kernel = None, locs = None,
        warmup_steps = 5, **kwargs):
    def f(*args):
        return cfg['inference_model'](*args, scale = noise_scale, alpha = alpha, N = N)

    if kernel is None:
        kernel = NUTS(f)
    mcmc = MCMC(kernel, num_samples=num_samples, warmup_steps=warmup_steps)
    mcmc.run(data)
    posterior_samples = mcmc.get_samples()

    local_weights = posterior_samples['phase_weights']
    if locs is None:#hack
        locs = posterior_samples['locs']
    scales = posterior_samples['scales']
    L_omega = posterior_samples['L_omega']

    L_Omega = torch.einsum('mij,mlij -> mlij',
         torch.einsum('ij,mj -> mij', torch.eye(ndim), scales.sqrt()),
         torch.einsum('ljik,lnj->lnik', L_omega, local_weights))

    weighted_expectation = torch.einsum('kji,klj->kli', locs, local_weights)

    log_likelihood = (dist.MultivariateNormal(weighted_expectation, scale_tril=L_Omega)
              .log_prob(data).sum().item())

    return {'predictive': posterior_samples,
        'model': f,
        'losses': [None],
        'log_likelihoods': [log_likelihood]}

class Run(object):
    """
    If datadict is provided, use it as a sample set. Otherwise generate
    observations using the given alpha, noise_scale and N
    """
    def __init__(self, alpha, datadict = None, num_samples = 100, N = N, noise_scale = .01,
                inference_posterior_fn = mcmc_posterior, inference_seed = None,
                infer_noise_scale = .01, n_warmup = 0, warmup = True):
        # set this if you want the same cluster params independent of alpha
        #pyro.set_rng_seed(3)

        # TODO hyperparameter optimization?
        if datadict is None:
            we, locs, data, weights_ground, gen_data_dict = gen_data(N, alpha = alpha, noise_scale = noise_scale)
        else:
            we, locs, data, weights_ground, gen_data_dict = datadict['latents'], datadict['locs'], datadict['data'], datadict['weights'], datadict['samples']

        self.we = we
        self.weights = weights_ground
        self.locs = locs
        self.data = data
        self.data_param_dict = gen_data_dict
        self.num_samples = num_samples
        self.N = N
        self.alpha = alpha
        self.inference_seed = inference_seed
        self.noise_scale = noise_scale
        self.infer_noise_scale = infer_noise_scale
        self.inference_posterior_fn = inference_posterior_fn

        if inference_seed is not None:
            pyro.set_rng_seed(inference_seed)

        if warmup:
            self.warmup(n_iter = n_warmup)
            pyro.clear_param_store()

    def get_loglikelihood(self, num_samples = 50):
        return get_log_likelihood(self.inference_output['model'],
                                  self.inference_output['guide'],
                                  self.data,
                                  num_samples = num_samples)

    def warmup(self, n_iter = n_iter,
           inference_posterior_fn = mcmc_posterior):
        self.inference_output = self.inference_posterior_fn(self.data, self.num_samples, self.N,
                                                self.alpha, noise_scale = self.infer_noise_scale,
                                                n_iter = n_iter, seed = self.inference_seed,
                                                n_likelihood_samples = num_samples)
        return self

    def run(self, n_iter = n_iter):
        if self.inference_seed is not None:
            pyro.set_rng_seed(self.inference_seed)

        self.warmup(n_iter = n_iter)
        we = self.we
        locs = self.locs
        data = self.data
        inference_output = self.inference_output

        posterior_samples = inference_output['predictive']
        wrapped_model = inference_output['model']

        components = [posterior_samples["locs"][:, i, :] for i in range(T)]

        posterior_locs = np.array(posterior_samples["locs"])
        locs_posterior_means = np.vstack([loc_means(posterior_locs[:, i, :]) for i in range(posterior_locs.shape[1])])

        # TODO generalize to K > 3
        va, vb, vc = locs
        beta = get_beta(va, vb, vc)

        rms_locs = rms([loc_stds(posterior_locs[:, i, :]) for i in range(posterior_locs.shape[1])])

        # TODO refactor
        # here we figure out mapping from ground truth end member indices to inferred end member indices
        clust_permutations = list(permutations(np.arange(T), r = T))
        permutation_norms = [np.linalg.norm(locs_posterior_means[ci_permute, :] - np.array(locs))
                             for ci_permute in clust_permutations]
        best_permutation = clust_permutations[np.argmin(permutation_norms)]
        locs_diffs = locs_posterior_means[best_permutation, :] - np.array(locs)

        # reshuffle random seed
        pyro.set_rng_seed(np.random.randint(1e9))

        # merge with the posterior samples dictionary, instead of extracting sites
        # individually
        result_dict = {'data': data, 'locs': locs, 'samples': posterior_samples, 'rms_locs': rms_locs,
                      'diff_locs': locs_diffs, 'permutation': best_permutation, 'alpha': self.alpha, 'beta': beta,
                      'components': components, 'latents': we, 'weights': self.weights, 'noise_scale': self.noise_scale,
                      'model': wrapped_model, 'centroid_mu_posterior': locs_posterior_means,
                      'posterior_locs': posterior_locs,
                      'inference_output': inference_output, 'data_param_dict': self.data_param_dict}
        return result_dict

def nfindr_locs(data):
    data = np.array(data).copy()
    data = np.array(data)[:, None, :]
    nfindr = eea.NFINDR()
    U = nfindr.extract(data, T, maxit = 1500, normalize=False, ATGP_init=False)
    return U

def score_nfindr(elt):
    """
    Get end members using nfindr and return the endmember coordinate errors
    """
    locs = elt['locs']
    data = elt['data']
    return closest_permutation_diffs(nfindr_locs(data), locs)

def plotnfindr(i, save = False, xlim = None, ylim = None):
    U= get_max(nfindr_locs, lambda inp: get_beta(*inp, norm = False), res_noise[i]['data'])
    plt.scatter(*(res_noise[i]['data']).T, s = 1, label = 'phase embeddings')
    plt.scatter(*(res_noise[i]['locs'].T), s = 50, label = 'end members (ground truth)')
    plt.scatter(*U.T, label = 'nfindr')

    plt.legend(loc = 'upper left')
    #plt.title('alpha = {:.2f}; beta = {:.2f}'.format(alphas[i], betas[i]))
    if xlim:
        plt.xlim(xlim)
    if ylim:
        plt.ylim(ylim)
    if save:
        plt.savefig('data/figs/{}.png'.format(i))

def get_max(f, metric, *args, attempts = 1):
    res = []
    for _ in range(attempts):
        out = f(*args)
        score = metric(out)
        res.append((score, out))
    best = sorted(res, key = lambda tup: tup[0])[-1]
    return best[1]

def grid_generate(alphas, noise_scales):
    start_seed = 1
    pyro.set_rng_seed(start_seed)
    ndim = T - 1
    runs = []
    res = []
    for data_seed, (a, noise_scale) in enumerate(zip(alphas, noise_scales)):
        print(noise_scale)
#        loss, run, seed = get_inference_seed(data_seed, a, num_samples = 100, N = 500,
#                                    noise_scale= noise_scale,
#                                    infer_noise_scale = noise_scale,
#                                    inference_posterior_fn=vi_inference)
        run = Run(a, noise_scale = noise_scale, inference_posterior_fn=vi_inference, N = N,
                    warmup = False)
        runs.append(run)
        res.append(run.run())
    return runs, res

def gridscan_alpha(alphas = np.logspace(-1, 1, 10), noise_scale = .03 * .5 + 1e-3, random_scale = False):
    if random_scale:
        scales = np.random.uniform(size = len(alphas)) * noise_scale * 2
    else:
        scales = np.repeat(noise_scale, len(alphas))
    runs, runoutputs = grid_generate(alphas, scales)
    return alphas, scales, runs, runoutputs

def gridscan_noise(alpha = 1, noise_scales = .03 * np.random.uniform(10) + 1e-3):
    alphas = np.repeat(alpha, len(noise_scales))
    runs, runoutputs = grid_generate(alphas, noise_scales)
    return alphas, scales, runs, runoutputs

####
# plotting functions
####

def plt_heatmap_alpha_noise(runoutputs, alphas, zname = 'diff_locs', yscale = 300, label = None, vmax = 1):
    """
    Plot a heatmap of reconstruction errors.
    """
    noise = np.array([elt['noise_scale'] for elt in runoutputs])

    x = alphas
    y = noise * yscale
    z = np.array([np.linalg.norm(elt[zname]) for elt in runoutputs])

    X = np.logspace(np.log10(min(x)), np.log10(max(x)))
    Y = np.linspace(min(y), max(y))
    X, Y = np.meshgrid(X, Y)  # 2D grid for interpolation
    #interp = CloughTocher2DInterpolator(list(zip(x, y)), z)
    interp = NearestNDInterpolator(list(zip(x, y)), z)
    Z = interp(X, Y)
    plt.pcolormesh(X, Y, Z, shading='auto', cmap = 'jet', vmin = 0, vmax = vmax)
    plt.plot(x, y, "ok")#, label="input point")
    plt.legend()
    plt.colorbar()
    #plt.axis("equal")
    plt.xlabel('$\\alpha_1$')
    plt.ylabel('$\\sigma$')
    plt.title(label)
    plt.semilogx()
    #plt.show()
    return z

def ploti2(runoutputs, i, save = False, xlim = None, ylim = None, with_nfindr = False, loc = 'lower right'):
    """
    Plot end member extraction results for a single dataset
    """
    locs = runoutputs[i]['locs']
    data = runoutputs[i]['data']
    plt.scatter(*(data).T, s = 5, label = 'Training observations')
    plt.scatter(*torch.vstack(runoutputs[i]['components']).T, s = 5, label = 'End members\n(posterior samples)')

    plt.scatter(*(locs.T), s = 50, label = 'End members\n(ground truth)')

    post_loc_centroids = np.vstack([comp.mean(axis = 0) for comp in runoutputs[i]['components']])
    plt.scatter(*post_loc_centroids.T, c = 'k', s = 50, label = 'End members (posterior\nsample centroids)')
    if with_nfindr:
        plt.scatter(*(nfindr_locs(data).T), s = 50, label = 'End members (N-FINDR)')

    #plt.legend()
    plt.legend(loc = loc)

    #plt.title('alpha = {:.2f}; beta = {:.2f}'.format(alphas[i], betas[i]))
    if xlim:
        plt.xlim(xlim)
    if ylim:
        plt.ylim(ylim)
    if save:
        plt.savefig('data/figs/{}.png'.format(i))


##########
# Stuff below here is currently not being used
##########

def initialize(seed, *args, **kwargs):
    global global_guide, svi, prior_sample

    def f():
        pyro.clear_param_store()
        if seed is not None:
            pyro.set_rng_seed(seed + 1000) # add offset to avoid collision with other places where we're setting the seed
        return Run(*args, **kwargs)
    run = f()
    vi_init = run.inference_output
    ll = run.get_loglikelihood(num_samples = 50)
    print(ll)
    return ll, run

def get_inference_seed(data_seed, *args, **kwargs):
    (loss, run), seed = max((initialize(data_seed, *args, **kwargs,
          inference_seed = s), s) for s in range(10))
    return loss, run, seed


import functools
import warnings

import matplotlib.pyplot as plt
import numpy as np
import seaborn as sns

import tensorflow.compat.v2 as tf
import tensorflow_probability as tfp

from tensorflow_probability import bijectors as tfb
from tensorflow_probability import distributions as tfd

tf.enable_v2_behavior()

plt.style.use("ggplot")
warnings.filterwarnings('ignore')

