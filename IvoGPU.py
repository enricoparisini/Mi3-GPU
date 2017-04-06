#!/usr/bin/env python2
#
#Copyright 2016 Allan Haldane.

#This file is part of IvoGPU.

#IvoGPU is free software: you can redistribute it and/or modify
#it under the terms of the GNU General Public License as published by
#the Free Software Foundation, version 3 of the License.

#IvoGPU is distributed in the hope that it will be useful,
#but WITHOUT ANY WARRANTY; without even the implied warranty of
#MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#GNU General Public License for more details.

#You should have received a copy of the GNU General Public License
#along with IvoGPU.  If not, see <http://www.gnu.org/licenses/>.

#Contact: allan.haldane _AT_ gmail.com
from __future__ import print_function
from scipy import *
from scipy.misc import logsumexp
import scipy
import numpy as np
from numpy.random import randint, shuffle
import pyopencl as cl
import pyopencl.array as cl_array
import sys, os, errno, argparse, time, ConfigParser
import seqload
from changeGauge import zeroGauge, zeroJGauge, fieldlessGaugeEven
from mcmcGPU import setupGPUs, initGPU, divideWalkers, printGPUs, readGPUbufs
from NewtonSteps import newtonMCMC, runMCMC, swapTemps

################################################################################
# Set up enviroment and some helper functions

progname = 'IvoGPU.py'

def mkdir_p(path):
    try:
        os.makedirs(path)
    except OSError as exc:
        if not (exc.errno == errno.EEXIST and os.path.isdir(path)):
            raise
scriptPath = os.path.dirname(os.path.realpath(__file__))
printsome = lambda a: " ".join(map(str,a.flatten()[:5]))

class attrdict(dict):
    def __getattr__(self, attr):
        try:
            return dict.__getitem__(self, attr)
        except KeyError:
            return None

def seqsize_from_param_shape(shape):
    L = int(((1+sqrt(1+8*shape[0]))/2) + 0.5)
    q = int(sqrt(shape[1]) + 0.5)
    return L, q

#identical calculation as CL kernel, but with high precision (to check fp error)
def getEnergiesMultiPrec(s, couplings):
    from mpmath import mpf, mp
    mp.dps = 32
    couplings = [[mpf(float(x)) for x in r] for r in couplings]
    pairenergy = [mpf(0) for n in range(s.shape[0])]
    for n,(i,j) in enumerate([(i,j) for i in range(L-1) for j in range(i+1,L)]):
        r = couplings[n]
        cpl = (r[b] for b in (q*s[:,i] + s[:,j]))
        pairenergy = [x+n for x,n in zip(pairenergy, cpl)]
    return pairenergy

def unimarg(bimarg):
    L, q = seqsize_from_param_shape(bimarg.shape)
    ff = bimarg.reshape((L*(L-1)/2,q,q))
    f = (array([sum(ff[0],axis=1)] + [sum(ff[n],axis=0) for n in range(L-1)]))
    return f/(sum(f,axis=1)[:,newaxis]) # correct any fp errors

def indep_bimarg(bimarg):
    f = unimarg(bimarg)
    L = f.shape[0]
    return array([outer(f[i], f[j]).flatten() for i in range(L-1)
                                    for j in range(i+1,L)])

################################################################################

def optionRegistry():
    options = []
    add = lambda opt, **kwds: options.append((opt, kwds))

    # option used by both potts and sequence loaders, designed
    # to load in the output of a previous run
    add('seqmodel', default=None,
        help=("One of 'zero', 'logscore', or a directory name. Generates or "
              "loads 'alpha', 'couplings', 'startseq' and 'seqs', if not "
              "otherwise supplied.") )
    add('outdir', default='output', help='Output Directory')

    # GPU options
    add('nwalkers', type=uint32,
        help="Number of MC walkers")
    add('nsteps', type=uint32, default=2048,
        help="number of MC steps per kernel call")
    add('wgsize', type=int, default=256,
        help="GPU workgroup size")
    add('gpus',
        help="GPUs to use (comma-sep list of platforms #s, eg '0,0')")
    add('profile', action='store_true',
        help="enable OpenCL profiling")
    add('nlargebuf', type=uint32, default=1,
        help='size of large seq buffer, in multiples of nwalkers')
    add('measurefperror', action='store_true',
        help="enable fp error calculation")
    add('gibbs', action='store_true',
        help='Use gibbs sampling instead of metropoils-hastings')

    # Newton options
    add('bimarg', required=True,
        help="Target bivariate marginals (npy file)")
    add('mcsteps', type=uint32, required=True,
        help="Number of rounds of MCMC generation")
    add('newtonsteps', default='128', type=uint32,
        help="Number of newton steps per round.")
    add('gamma', type=float32, required=True,
        help="Initial step size")
    add('damping', default=0.001, type=float32,
        help="Damping parameter")
    add('noiseN', default=None,
        help="effective MSA size for anti-overfitting noise")
    add('Creg', default=None,
        help="C matrix to use in l1 regularization")
    add('preopt', action='store_true',
        help="Perform a round of newton steps before first MCMC run")
    add('resetseqs', action='store_false',
        help="Reset sequence to S0 at start of every MCMC round")

    # Potts options
    add('alpha', required=True,
        help="Alphabet, a sequence of letters")
    add('couplings',
        help="One of 'zero', 'logscore', or a filename")
    add('L', help="sequence length", type=int)

    # Sequence options
    add('startseq', help="Starting sequence. May be 'rand'")
    add('seqs', help="File containing sequences to pre-load to GPU")

    # Sampling Param
    add('preequiltime', type=uint32, default=0,
        help="Number of MC kernel calls to run before newton steps")
    add('equiltime', type=uint32, required=True,
        help="Number of MC kernel calls to equilibrate")
    add('sampletime', type=uint32, required=True,
        help="Number of MC kernel calls between samples")
    add('nsamples', type=uint32, required=True,
        help="Number of sequence samples")
    add('trackequil', type=uint32, default=0,
        help='Save bimarg every TRACKEQUIL steps during equilibration')
    add('tempering',
        help='optional inverse Temperature schedule')
    add('nswaps_temp', type=uint32, default=10000,
        help='optional number of pt swaps')

    return dict(options)

def addopt(parser, groupname, optstring):
    if groupname is not None:
        group = parser.add_argument_group(groupname)
        add = group.add_argument
    else:
        add = parser.add_argument

    for option in optstring.split():
        optargs = addopt.options[option]
        add('--' + option, **optargs)
addopt.options = optionRegistry()

def requireargs(args, required):
    required = required.split()
    args = vars(args)
    for r in required:
        if args[r] is None:
            raise Exception("error: argument --{} is required".format(r))

################################################################################

def inverseIsing(args, log):
    descr = ('Inverse Ising inference using a quasi-Newton MCMC algorithm '
             'on the GPU')
    parser = argparse.ArgumentParser(prog=progname + ' inverseIsing',
                                     description=descr)
    addopt(parser, 'GPU options',         'nwalkers nsteps wgsize '
                                          'gibbs gpus profile')
    addopt(parser, 'Sequence Options',    'startseq seqs')
    addopt(parser, 'Newton Step Options', 'bimarg mcsteps newtonsteps gamma '
                                          'damping Creg noiseN '
                                          'preopt resetseqs')
    addopt(parser, 'Sampling Options',    'equiltime sampletime nsamples '
                                          'trackequil tempering nswaps_temp '
                                          'preequiltime')
    addopt(parser, 'Potts Model Options', 'alpha couplings L')
    addopt(parser,  None,                 'seqmodel outdir')

    args = parser.parse_args(args)
    args.nlargebuf = args.nsamples
    args.measurefperror = False

    log("Initialization")
    log("===============")
    log("")

    p = attrdict({'outdir': args.outdir})
    mkdir_p(args.outdir)

    p.update(process_newton_args(args, log))
    if p.bimarg is not None:
        p['L'], p['q'] = seqsize_from_param_shape(p.bimarg.shape)

    p.update(process_potts_args(args, p.L, p.q, p.bimarg, log))
    L, q, alpha = p.L, p.q, p.alpha

    p.update(process_sample_args(args, log))
    gpup, cldat, gdevs = process_GPU_args(args, L, q, p.outdir, log)
    p.update(gpup)
    gpuwalkers = divideWalkers(p.nwalkers, len(gdevs), p.wgsize, log)
    gpus = [initGPU(n, cldat, dev, nwalk, p, log)
            for n,(dev, nwalk) in enumerate(zip(gdevs, gpuwalkers))]

    rngPeriod = (p.equiltime + p.sampletime*p.nsamples)*p.mcmcsteps
    for gpu in gpus:
        gpu.initMCMC(p.nsteps, rngPeriod)
        gpu.initLargeBufs(gpu.nseq['main']*p.nsamples)
        gpu.initJstep()
        if p.tempering:
            gpu.initMarkSeq()
    log("")
    #log(("Running {} MCMC walkers in parallel over {} GPUs, with {} MC "
    #    "steps per kernel call").format(p.nwalkers, len(gpus),
    #    p.nsteps))

    preopt_seqs = sum([g.nseq['large'] for g in gpus])
    p.update(process_sequence_args(args, L, alpha, p.bimarg, log,
                                   nseqs=preopt_seqs))
    if p.preopt:
        if p.seqs is None:
            raise Exception("Need to provide seqs if using pre-optimization")
        transferSeqsToGPU(gpus, 'large', p.seqs, log)

    #if we're not initializing seqs to single sequence, need to load
    # a set of initial sequences into main buffer
    if not p.resetseqs:
        if p.seqs is None:
            raise Exception("Need to provide seqs if not using startseq")
        #get required seqs from end of detected seqs
        if len(p.seqs) == 1:
            mainseq = [p.seqs[-sum([g.nseq['main'] for f in gpus]):]]
        else:
            mainseq = [s[-g.nseq['main']:] for s,g in zip(p.seqs, gpus)]
        transferSeqsToGPU(gpus, 'main', mainseq, log)
    log("")

    log("Computation Overview")
    log("====================")
    log("Running {} Newton-MCMC rounds, with {} parameter update steps per "
        "round.".format(p.mcmcsteps, p.newtonSteps))
    log(("In each round, running {} MC walkers for {} equilibration loops then "
         "sampling every {} loops to get {} samples ({} total seqs) with {} MC "
         "steps per loop (Each walker equilibrated a total of {} MC steps)."
         ).format(p.nwalkers, p.equiltime, p.sampletime, p.nsamples,
                p.nsamples*p.nwalkers, p.nsteps, p.nsteps*p.equiltime))
    if p.tempering is not None:
        log("Parallel tempering: The walkers are divided into {} temperature "
            "groups ({}), and temperatures are swapped {} times after every "
            "MCMC loop".format(len(p.tempering), args.tempering, p.nswaps))

    log("")
    log("")
    log("MCMC Run")
    log("========")

    newtonMCMC(p, gpus, log)

def getEnergies(args, log):
    descr = ('Compute Potts Energy of a set of sequences')
    parser = argparse.ArgumentParser(prog=progname + ' getEnergies',
                                     description=descr)
    add = parser.add_argument
    add('out', default='output', help='Output File')
    addopt(parser, 'GPU Options',         'wgsize gpus profile')
    addopt(parser, 'Potts Model Options', 'alpha couplings')
    addopt(parser, 'Sequence Options',    'seqs')
    addopt(parser,  None,                 'outdir')

    #genenergies uses a subset of the full inverse ising parameters,
    #so use custom set of params here

    args = parser.parse_args(args)
    args.measurefperror = False

    requireargs(args, 'couplings alpha seqs')

    log("Initialization")
    log("===============")
    log("")

    p = attrdict({'outdir': args.outdir})
    mkdir_p(args.outdir)
    p.update(process_potts_args(args, None, None, None, log))
    L, q, alpha = p.L, p.q, p.alpha
    log("Sequence Setup")
    log("--------------")
    seqs = loadSequenceFile(args.seqs, alpha, log)
    if seqs is None:
        raise Exception("seqs must be supplied")
    log("")

    args.nwalkers = len(seqs)
    args.gibbs = False
    args.nsteps = 1
    args.nlargebuf = 1
    gpup, cldat, gdevs = process_GPU_args(args, L, q, p.outdir, 1, log)
    p.update(gpup)
    gpuwalkers = divideWalkers(p.nwalkers, len(gdevs), p.wgsize, log)
    gpus = [initGPU(n, cldat, dev, nwalk, 1, p, log)
            for n,(dev, nwalk) in enumerate(zip(gdevs, gpuwalkers))]
    transferSeqsToGPU(gpus, 'main', [seqs], log)
    log("")


    log("Computing Energies")
    log("==================")

    for gpu in gpus:
        gpu.setBuf('J main', p.couplings)

    for gpu in gpus:
        gpu.calcEnergies('main', 'main')
    es = concatenate(readGPUbufs(['E main'], gpus)[0])

    log("Saving results to file '{}'".format(args.out))
    save(args.out, es)


def MCMCbenchmark(args, log):
    descr = ('Benchmark MCMC generation on the GPU')
    parser = argparse.ArgumentParser(prog=progname + ' benchmark',
                                     description=descr)
    add = parser.add_argument
    add('--nloop', type=uint32, required=True,
        help="Number of kernel calls to benchmark")
    addopt(parser, 'GPU options',         'nwalkers nsteps wgsize '
                                          'gibbs gpus profile')
    addopt(parser, 'Sequence Options',    'startseq seqs')
    addopt(parser, 'Potts Model Options', 'alpha couplings L')
    addopt(parser,  None,                 'seqmodel outdir')

    args = parser.parse_args(args)
    nloop = args.nloop
    args.measurefperror = False

    log("Initialization")
    log("===============")
    log("")

    p = attrdict({'outdir': args.outdir})
    mkdir_p(args.outdir)
    p.update(process_potts_args(args, p.L, p.q, None, log))
    L, q, alpha = p.L, p.q, p.alpha
    args.nlargebuf = 1
    gpup, cldat, gdevs = process_GPU_args(args, L, q, p.outdir, log)
    p.update(gpup)
    gpuwalkers = divideWalkers(p.nwalkers, len(gdevs), p.wgsize, log)
    gpus = [initGPU(n, cldat, dev, nwalk, p, log)
            for n,(dev, nwalk) in enumerate(zip(gdevs, gpuwalkers))]
    for gpu in gpus:
        gpu.initMCMC(p.nsteps, 2*nloop)
    log("")
    preopt_seqs = sum([g.nseq['main'] for g in gpus])
    p.update(process_sequence_args(args, L, alpha, None, log,nseqs=preopt_seqs))

    if p.seqs is not None:
        transferSeqsToGPU(gpus, 'main', p.seqs, log)
    elif p.startseq is not None:
        log("Loading main seq buffer with startseq")
        for gpu in gpus:
            gpu.fillSeqs(p.startseq)
    else:
        raise Exception("Error: To benchmark, must either supply startseq or "
                        "load seqs into main seq buffer")
    log("")


    log("Benchmark")
    log("=========")
    log("")
    log("Benchmarking MCMC for {} loops, {} MC steps per loop".format(
                                                 nloop, p.nsteps))
    import time

    def runMCMC():
        for i in range(nloop):
            for gpu in gpus:
                gpu.runMCMC()
        for gpu in gpus:
            gpu.wait()

    #initialize
    for gpu in gpus:
        gpu.setBuf('J main', p.couplings)

    #warmup
    log("Warmup run...")
    runMCMC()

    #timed run
    log("Timed run...")
    start = time.clock()
    runMCMC()
    end = time.clock()

    log("Elapsed time: ", end - start, )
    totsteps = p.nwalkers*nloop*p.nsteps
    steps_per_second = totsteps/(end-start)
    log("MC steps computed: {}".format(totsteps))
    log("MC steps per second: {:g}".format(steps_per_second))

def equilibrate(args, log):
    descr = ('Run a round of MCMC generation on the GPU')
    parser = argparse.ArgumentParser(prog=progname + ' mcmc',
                                     description=descr)
    add = parser.add_argument
    addopt(parser, 'GPU options',         'nwalkers nsteps wgsize '
                                          'gibbs gpus profile')
    addopt(parser, 'Sequence Options',    'startseq seqs')
    addopt(parser, 'Sampling Options',    'equiltime sampletime nsamples '
                                          'trackequil tempering nswaps_temp')
    addopt(parser, 'Potts Model Options', 'alpha couplings L')
    addopt(parser,  None,                 'seqmodel outdir')

    args = parser.parse_args(args)
    args.measurefperror = False

    log("Initialization")
    log("===============")

    p = attrdict({'outdir': args.outdir})
    mkdir_p(args.outdir)

    p.update(process_potts_args(args, None, None, None, log))
    L, q, alpha = p.L, p.q, p.alpha

    p.update(process_sample_args(args, log))
    rngPeriod = (p.equiltime + p.sampletime*p.nsamples)
    gpup, cldat, gdevs = process_GPU_args(args, L, q, p.outdir, log)
    p.update(gpup)
    gpuwalkers = divideWalkers(p.nwalkers, len(gdevs), p.wgsize, log)
    gpus = [initGPU(n, cldat, dev, nwalk, p, log)
            for n,(dev, nwalk) in enumerate(zip(gdevs, gpuwalkers))]
    for gpu in gpus:
        gpu.initMCMC(p.nsteps, rngPeriod)
        gpu.initLargeBufs(gpu.nseq['main']*p.nsamples)
        if p.tempering:
            gpu.initMarkSeq()
    preopt_seqs = sum([g.nseq['large'] for g in gpus])
    p.update(process_sequence_args(args, L, alpha, None, log,
                                   nseqs=preopt_seqs))
    log("")

    log("Equilibrating ...")
    for gpu in gpus:
        gpu.fillSeqs(p.startseq)

    # set up tempering if needed
    if p.tempering is not None:
        B0 = p.tempering[0]

        if p.nwalkers % len(p.tempering) != 0:
            raise Exception("# of temperatures must evenly divide # walkers")
        Bs = concatenate([ones(p.nwalkers/len(p.tempering), dtype='f4')*b 
                          for b in p.tempering])
        shuffle(Bs)
        Bs = split(Bs, len(gpus)) 
        for B,gpu in zip(Bs, gpus):
            gpu.setBuf('Bs', B)
            gpu.markSeqs(B == B0)
    
    # actually run the mcmc
    (bimarg_model, 
     bicount, 
     energies, 
     seqs) = runMCMC(gpus, p.startseq, p.couplings, '.', p)
    
    outdir = p.outdir
    savetxt(os.path.join(outdir, 'bicounts'), bicount, fmt='%d')
    save(os.path.join(outdir, 'bimarg'), bimarg_model)
    save(os.path.join(outdir, 'energies'), energies)
    for n,seqbuf in enumerate(seqs):
        seqload.writeSeqs(os.path.join(outdir, 'seqs-{}'.format(n)),
                          seqbuf, alpha)
    for gpu in gpus:
        gpu.calcEnergies('main', 'main')
    se, ss, sB = readGPUbufs(['E main', 'seq main', 'Bs'], gpus)
    seqload.writeSeqs(os.path.join(outdir, 'mainseqs'), concatenate(ss), alpha)
    save(os.path.join(outdir, 'mainE'), concatenate(se))
    save(os.path.join(outdir, 'mainB'), concatenate(sB))

    #slarge = gpus[-1].getBuf('seq large', False).read()
    #seqload.writeSeqs('gpu3seqbuf', slarge, alpha)

    log("Done!")

def equil_PT(args, log):
    descr = ('Run MCMC equilibration on the GPU with parallel tempering')
    parser = argparse.ArgumentParser(prog=progname + ' equil_pt',
                                     description=descr)
    add = parser.add_argument
    addopt(parser, 'GPU options',         'nwalkers nsteps wgsize '
                                          'gibbs gpus profile')
    addopt(parser, 'Sequence Options',    'startseq seqs')
    addopt(parser, 'Sampling Options',    'equiltime trackequil tempering '
                                          'nswaps_temp')
    addopt(parser, 'Potts Model Options', 'alpha couplings L')
    addopt(parser,  None,                 'seqmodel outdir')

    args = parser.parse_args(args)
    args.measurefperror = False

    log("Initialization")
    log("===============")

    p = attrdict({'outdir': args.outdir})
    mkdir_p(args.outdir)

    p.update(process_potts_args(args, None, None, None, log))
    L, q, alpha = p.L, p.q, p.alpha
    
    args.sampletime = 1
    args.nsamples = 1
    p.update(process_sample_args(args, log))
    rngPeriod = (p.equiltime + p.sampletime*p.nsamples)
    gpup, cldat, gdevs = process_GPU_args(args, L, q, p.outdir, log)
    p.update(gpup)
    gpuwalkers = divideWalkers(p.nwalkers, len(gdevs), p.wgsize, log)
    gpus = [initGPU(n, cldat, dev, nwalk, p, log)
            for n,(dev, nwalk) in enumerate(zip(gdevs, gpuwalkers))]
    preopt_seqs = sum([g.nseq['main'] for g in gpus])
    p.update(process_sequence_args(args, L, alpha, None, log,
                                   nseqs=preopt_seqs))
    for gpu in gpus:
        gpu.initMCMC(p.nsteps, rngPeriod)
    log("")
    
    # set up gpu buffers
    if p.seqs is not None:
        transferSeqsToGPU(gpus, 'main', p.seqs, log)
    elif p.startseq is not None:
        log("Loading main seq buffer with startseq")
        for gpu in gpus:
            gpu.fillSeqs(p.startseq)
    else:
        raise Exception("Error: Must either supply startseq or seqs")

    for gpu in gpus:
        gpu.setBuf('J main', p.couplings)

    if p.nwalkers % len(p.tempering) != 0:
        raise Exception("# of temperatures must evenly divide # walkers")
    Bs = concatenate([ones(p.nwalkers/len(p.tempering), dtype='f4')*b 
                      for b in p.tempering])
    log("Using {} beta from {} to {}".format(
               len(p.tempering), max(Bs), min(Bs)))
    for B,gpu in zip(split(Bs, len(gpus)), gpus):
        gpu.setBuf('Bs', B)
    log("")

    outdir = p.outdir
    trackequil = p.trackequil
    nloop = p.equiltime
    
    # actually run the mcmc
    def getPTBufs(gpus):
        return map(concatenate, readGPUbufs(['E main', 'seq main', 'Bs'], gpus))

    def writePTBufs(dir, energies, seqs, Bs):
        save(os.path.join(dir, 'energies'), energies)
        save(os.path.join(dir, 'Bs'), Bs)
        seqload.writeSeqs(os.path.join(dir, 'seqs'), seqs, p.alpha)

    log("Equilibrating ...")
    if trackequil == 0:
        trackequil = nloop

    for j in range(nloop/trackequil):
        for i in range(trackequil):
            for gpu in gpus:
                gpu.runMCMC()
            swapTemps(gpus, p.nswaps)
            log("Step {} done".format(i))
        
        if j != (nloop/trackequil - 1): # don't write last one, written below
            dir = os.path.join(outdir, 'equilibration', str(j*trackequil))
            mkdir_p(dir)
            writePTBufs(dir, *getPTBufs(gpus))

    writePTBufs(outdir, *getPTBufs(gpus))

    log("Done!")

def subseqFreq(args, log):
    descr = ('Compute relative frequency of subsequences at fixed positions')
    parser = argparse.ArgumentParser(prog=progname + ' subseqFreq',
                                     description=descr)
    add = parser.add_argument
    add('fixpos', help="comma separated list of fixed positions")
    add('out', default='output', help='Output File')
    addopt(parser, 'GPU options',         'wgsize gpus')
    addopt(parser, 'Potts Model Options', 'alpha couplings L')
    addopt(parser,  None,                 'outdir')
    group = parser.add_argument_group('Sequence Options')
    add = group.add_argument
    add('backgroundseqs')
    add('subseqs')

    args = parser.parse_args(args)
    args.measurefperror = False

    log("Initialization")
    log("===============")
    log("")

    p = attrdict({'outdir': args.outdir})
    args.trackequil = 0
    mkdir_p(args.outdir)
    p.update(process_potts_args(args, p.L, p.q, None, log))
    L, q, alpha = p.L, p.q, p.alpha

    # try to load sequence files
    bseqs = loadSequenceFile(args.backgroundseqs, alpha, log)
    sseqs = loadSequenceFile(args.subseqs, alpha, log)

    args.nwalkers = 1
    gpup, cldat, gdevs = process_GPU_args(args, L, q, p.outdir, 1, log)
    p.update(gpup)
    p.nsteps = 1
    gpuwalkers = divideWalkers(len(bseqs), len(gdevs), p.wgsize, log)
    gpus = [initGPU(n, cldat, dev, len(sseqs), nwalk, p, log)
            for n,(dev, nwalk) in enumerate(zip(gdevs, gpuwalkers))]

    #fix positions
    fixedpos = array([int(x) for x in args.fixpos.split(',')])
    fixedmarks = zeros(L, dtype='u1')
    fixedmarks[fixedpos] = 1

    #load buffers
    gpubseqs = split(bseqs, cumsum(gpuwalkers)[:-1])
    for gpu,bs in zip(gpus, gpubseqs):
        gpu.setBuf('seq main', sseqs)
        gpu.setBuf('seq large', bs)
        gpu.markPos(fixedmarks)
        gpu.setBuf('J main', p.couplings)
        
    log("")

    log("Subsequence Frequency Calculation")
    log("=================================")
    log("")

    for gpu in gpus:
        gpu.calcEnergies('large', 'main')
    origEs = concatenate(readGPUbufs(['E large'], gpus)[0])

    log("Getting substituted energies...")
    subseqE = []
    logf = zeros(len(sseqs))
    for n in range(len(sseqs)):
        # replaced fixed positions by subsequence, and calc energies
        for gpu in gpus:
            gpu.copySubseq(n)
            gpu.calcEnergies('large', 'main')
        energies = concatenate(readGPUbufs(['E large'], gpus)[0])
        logf[n] = logsumexp(origEs - energies)

    #save result
    log("Saving result (log frequency) to file {}".format(args.out))
    save(args.out, logf)

def nestedZ(args, log):
    raise Exception("Not implemented yet")
    # Plan is to implement nested sampling algorithm to compute Z.
    # Use parallel method described in
    #    Exploring the energy landscapes of protein folding simulations with
    #    Bayesian computation
    #    Nikolas S. Burkoff, Csilla Varnai, Stephen A. Wells and David L. Wild
    # we can probably do K = 1024, P = 256 or even better

def ExactZS(args, log):
    raise Exception("Not implemented yet")
    # plan is to implement exact solution of small systems by enumeration on
    # GPU. Would need to compute energy of all sequences, so kernel
    # would be similar to energy calculation kernel, except the actual
    # sequences would not need to be loaded from memory, but could
    # be computed on the fly. Z = sum(exp(-E)), and S = -sum(p*log(p))

def testing(args, log):
    parser = argparse.ArgumentParser(prog=progname + ' test',
                                     description="for testing")
    addopt(parser, 'GPU options',         'nwalkers wgsize gpus')
    addopt(parser, 'Newton Step Options', 'bimarg gamma damping')
    addopt(parser,  None,                 'outdir')

    args = parser.parse_args(args)
    args.measurefperror = False

    p = attrdict({'outdir': args.outdir})
    mkdir_p(args.outdir)
    outdir = p.outdir

    p['bimarg'] = scipy.load(args.bimarg)
    p['L'], p['q'] = seqsize_from_param_shape(p.bimarg.shape)
    L, q = p.L, p.q
    args.nsteps = 1
    args.gibbs = None
    args.profile = None
    args.fperror = None
    

    gpup, cldat, gdevs = process_GPU_args(args, L, q, p.outdir, log)
    p.update(gpup)
    gpuwalkers = divideWalkers(p.nwalkers, len(gdevs), p.wgsize, log)
    gpus = [initGPU(n, cldat, dev, nwalk, p, log)
            for n,(dev, nwalk) in enumerate(zip(gdevs, gpuwalkers))]

    for gpu in gpus:
        gpu.initLargeBufs(nwalk)
        gpu.initJstep()

    rbim = p.bimarg + 0.1*rand(*(p.bimarg.shape))
    rbim = (rbim/sum(rbim, axis=1)[:,newaxis]).astype('f4')
    for g in gpus:
        g.setBuf('bi target', p.bimarg)
        g.setBuf('bi back', rbim)

    for g in gpus:
        g.updateJ_Lstep(p.gamma, 0)

    res = readGPUbufs(['J front', 'J back'], gpus)
    dJ1, dJ2 = res[0][0], res[1][0]
    save(os.path.join(outdir, 'dJ1'), dJ1)
    save(os.path.join(outdir, 'dJ2'), dJ2)
    save(os.path.join(outdir, 'bitarget'), p.bimarg)
    save(os.path.join(outdir, 'binew'), rbim)

################################################################################

def process_GPU_args(args, L, q, outdir, log):
    log("GPU setup")
    log("---------")

    param = attrdict({'nsteps': args.nsteps,
                      'wgsize': args.wgsize,
                      'nwalkers': args.nwalkers,
                      'gpuspec': args.gpus,
                      'gibbs': args.gibbs,
                      'profile': args.profile,
                      'fperror': args.measurefperror})

    p = attrdict(param.copy())
    p.update({'L': L, 'q': q, 'outdir': outdir})

    scriptfile = os.path.join(scriptPath, "mcmc.cl")

    log("Work Group Size: {}".format(p.wgsize))
    log("{} MC steps per MCMC kernel call".format(p.nsteps))
    log("Using {} MC sampler".format('Gibbs' if args.gibbs
                                   else 'Metropolis-hastings'))
    log("GPU Initialization:")
    if p.profile:
        log("Profiling Enabled")
    clinfo, gpudevs = setupGPUs(scriptPath, scriptfile, p, log)

    log("")
    return p, clinfo, gpudevs

def process_newton_args(args, log):
    log("Newton Solver Setup")
    log("-------------------")
    mcmcsteps = args.mcsteps
    log("Running {} Newton-MCMC rounds".format(mcmcsteps))

    param = {'mcmcsteps': args.mcsteps,
             'newtonSteps': args.newtonsteps,
             'gamma0': args.gamma,
             'pcdamping': args.damping,
             'resetseqs': args.resetseqs,
             'preopt': args.preopt,
             'noiseN': args.noiseN if not args.noiseN else int(args.noiseN)}

    p = attrdict(param)

    log("Updating J locally with gamma={}, and pc-damping {}".format(
        p.gamma0, p.pcdamping))
    log("Running {} Newton update steps per round.".format(p.newtonSteps))

    log("Reading target marginals from file {}".format(args.bimarg))
    bimarg = scipy.load(args.bimarg)
    if bimarg.dtype != dtype('<f4'):
        raise Exception("Bimarg must be in 'f4' format")
        #could convert, but this helps warn that something may be wrong
    if any(~((bimarg.flatten() >= 0) & (bimarg.flatten() <= 1))):
        raise Exception("Bimarg must be nonzero and 0 < f < 1")
    log("Target Marginals: " + printsome(bimarg) + "...")
    p['bimarg'] = bimarg

    if args.Creg is not None:
        log("Regularizing with Creg from file {}".format(args.Creg))
        p['Creg'] = scipy.load(args.Creg)
        if p['Creg'].shape != bimarg.shape:
            raise Exception("Creg in wrong format")

    if p.noiseN:
        log("Adding MSA noise of size {} to step direction".format(p.noiseN))

    log("")
    return p

def updateLq(L, q, newL, newq, name):
    # update L and q with new values, checking that they
    # are the same as the old values if not None
    if newL is not None:
        if L is not None and L != newL:
            raise Exception("L from {} ({}) inconsitent with previous "
                            "value ({})".format(name, newL, L))
        L = newL
    if newq is not None:
        if q is not None and q != newq:
            raise Exception("q from {} ({}) inconsitent with previous "
                            "value ({})".format(name, newq, q))
        q = newq
    return L, q

def process_potts_args(args, L, q, bimarg, log):
    log("Potts Model Setup")
    log("-----------------")

    # we try to infer L and q from any values given. The possible sources
    # * command line options -L and -q
    # * from bivariate_target dimensions
    # * from coupling dimensions

    alpha = args.alpha
    L, q = updateLq(args.L, len(alpha), L, q, 'bimarg')

    # next try to get couplings (may determine L, q)
    couplings, L, q = getCouplings(args, L, q, bimarg, log)
    # we should have L and q by this point

    log("alphabet: {}".format(alpha))
    log("q {}  L {}".format(q, L))
    log("Couplings: " + printsome(couplings) + "...")

    log("")
    return attrdict({'L': L, 'q': q, 'alpha': alpha,
                     'couplings': couplings})

def getCouplings(args, L, q, bimarg, log):
    couplings = None

    if args.seqmodel and args.seqmodel in ['zero', 'logscore']:
        args.couplings = args.seqmodel

    if args.couplings:
        #first try to generate couplings (requires L, q)
        if args.couplings in ['zero', 'logscore']:
            if L is None: # we are sure to have q
                raise Exception("Need L to generate couplings")
        if args.couplings == 'zero':
            log("Setting Initial couplings to 0")
            couplings = zeros((L*(L-1)/2, q*q), dtype='<f4')
        elif args.couplings == 'logscore':
            log("Setting Initial couplings to Independent Log Scores")
            if bimarg is None:
                raise Exception("Need bivariate marginals to generate "
                                "logscore couplings")
            h = -np.log(unimarg(bimarg))
            J = zeros((L*(L-1)/2,q*q), dtype='<f4')
            couplings = fieldlessGaugeEven(h, J)[1]
        else: #otherwise load them from file
            log("Reading couplings from file {}".format(args.couplings))
            couplings = scipy.load(args.couplings)
            if couplings.dtype != dtype('<f4'):
                raise Exception("Couplings must be in 'f4' format")
    elif args.seqmodel and args.seqmodel not in ['zero', 'logscore']:
        # and otherwise try to load them from model directory
        fn = os.path.join(args.seqmodel, 'J.npy')
        if os.path.isfile(fn):
            log("Reading couplings from file {}".format(fn))
            couplings = scipy.load(fn)
            if couplings.dtype != dtype('<f4'):
                raise Exception("Couplings must be in 'f4' format")
    L2, q2 = seqsize_from_param_shape(couplings.shape)
    L, q = updateLq(L, q, L2, q2, 'couplings')

    if couplings is None:
        raise Exception("Could not find couplings. Use either the "
                        "'couplings' or 'seqmodel' options.")

    return couplings, L, q

def process_sequence_args(args, L, alpha, bimarg, log, nseqs=None):
    log("Sequence Setup")
    log("--------------")

    q = len(alpha)
    startseq, seqs = None, None

    # check if we were asked to generate sequences
    if any([arg in ['zero', 'logscore'] for arg in [args.seqmodel, args.seqs]]):
        if args.seqs is not None and args.seqmodel is not None:
            raise Exception("Cannot specify both seqs and "
                            "seqmodel=[rand, logscore]")
        if nseqs is None:
            raise Exception("Cannot generate sequences without known nseq")
        seqs = [generateSequences(args.seqmodel, L, q, nseqs, bimarg, log)]
        startseq = seqs[0][0]
        startseq_origin = 'generated ' + args.seqmodel
        seqmodeldir = None
    else:
        seqmodeldir = args.seqmodel

    # try to load sequence files
    if args.seqs not in [None, 'zero', 'logscore']:
        seqs = [loadSequenceFile(args.seqs, alpha, log)]
    elif seqmodeldir is not None:
        seqs = loadSequenceDir(seqmodeldir, alpha, log)

    # try to get start seq
    if args.startseq:
        if args.startseq == 'rand':
            startseq = randint(0, q, size=L).astype('<u1')
            startseq_origin = 'random'
        else: # given string
            startseq = array(map(alpha.index, args.startseq), dtype='<u1')
            startseq_origin = 'supplied'
    elif seqmodeldir is not None:
        fn = os.path.join(seqmodeldir, 'startseq')
        if os.path.isfile(fn):
            log("Reading startseq from file {}".format(fn))
            with open(fn) as f:
                startseq = f.readline().strip()
                startseq = array(map(alpha.index, startseq), dtype='<u1')
            startseq_origin = 'from file'

    if seqs is None:
        log("No sequence dataset loaded")

    if startseq is not None:
        log("Start seq ({}): {}".format(startseq_origin,
                                       "".join(alpha[x] for x in startseq)))
    else:
        log("No start seq supplied")

    log("")
    return attrdict({'startseq': startseq, 'seqs': seqs})

def generateSequences(gentype, L, q, nseqs, bimarg, log):
    if gentype == 'zero':
        log("Generating {} random sequences...".format(nseqs))
        return randint(0,q,size=(nseqs, L)).astype('<u1')
    elif gentype == 'logscore':
        log("Generating {} logscore-independent sequences...".format(nseqs))
        if bimarg is None:
            raise Exception("Bimarg must be provided to generate sequences")
        cumprob = cumsum(unimarg(bimarg), axis=1)
        cumprob = cumprob/(cumprob[:,-1][:,newaxis]) #correct fp errors?
        return array([searchsorted(cp, rand(nseqs)) for cp in cumprob],
                     dtype='<u1').T

def loadSequenceFile(sfile, alpha, log):
    log("Loading sequences from file {}".format(sfile))
    seqs = seqload.loadSeqs(sfile, names=alpha)[0].astype('<u1')
    return seqs

def loadSequenceDir(sdir, alpha, log):
    log("Loading sequences from dir {}".format(sdir))
    seqs = []
    while True:
        sfile = os.path.join(sdir, 'seqs-{}'.format(len(seqs)))
        if not os.path.exists(sfile):
            break
        seqs.append(seqload.loadSeqs(sfile, names=alpha)[0].astype('<u1'))
    return seqs

def transferSeqsToGPU(gpus, bufname, seqs, log):
    log("Transferring {} seqs to gpu's {} seq buffer...".format(str([len(s) for s in seqs]), bufname))
    if len(seqs) == 1:
        # split up seqs into parts for each gpu
        seqs = seqs[0]
        sizes = [g.nseq[bufname] for g in gpus]

        if len(seqs) == sum(sizes):
            seqs = split(seqs, cumsum(sizes)[:-1])
        else:
            raise Exception(("Expected {} total sequences, got {}").format(
                             sum(sizes), len(seqs)))

    for n,(gpu,seq) in enumerate(zip(gpus, seqs)):
        if len(seq) != gpu.nseq[bufname]:
            raise Exception("Expected {} sequences, got {}".format(
                            gpu.nseq[bufname], len(seq)))
        gpu.setBuf('seq ' + bufname, seq)

def process_sample_args(args, log):
    p = attrdict({'equiltime': args.equiltime,
                  'sampletime': args.sampletime,
                  'nsamples': args.nsamples,
                  'trackequil': args.trackequil})

    if 'tempering' in args and args.tempering:
        try:
            Bs = np.load(args.tempering)
        except:
            fls = [float(x) for x in args.tempering.split(",")]
            Bs = array(fls, dtype='f4')
        p['tempering'] = Bs
        p['nswaps'] = args.nswaps_temp
    if 'preequiltime' in args:
        p['preequiltime'] = args.preequiltime

    if p.nsamples == 0:
        raise Exception("nsamples must be at least 1")

    log("MCMC Sampling Setup")
    log("-------------------")
    log(("In each MCMC round, running {} GPU MCMC kernel calls then sampling "
         "every {} kernel calls to get {} samples").format(p.equiltime,
                                             p.sampletime, p.nsamples))
    if 'tempering' in p:
        log("Parallel tempering with temperatures {}".format(args.tempering))

    if p.trackequil != 0:
        if p.equiltime%p.trackequil != 0:
            raise Exception("Error: trackequil must be a divisor of equiltime")
        log("Tracking equilibration every {} loops.".format(p.trackequil))

    if p.preequiltime is not None and p.preequiltime != 0:
        log("Pre-equilibration for {} steps".format(p.preequiltime))

    log("")
    return p

################################################################################

class CLInfoAction(argparse.Action):
    def __init__(self, option_strings, dest=argparse.SUPPRESS,
                 default=argparse.SUPPRESS, help=None):
        super(CLInfoAction, self).__init__(option_strings=option_strings,
            dest=dest, default=default, nargs=0, help=help)
    def __call__(self, parser, namespace, values, option_string=None):
        printGPUs(print)
        parser.exit()

def readConfig(fp, section):
    config = ConfigParser.SafeConfigParser()
    config.readfp(fp)
    sections = config.sections()
    if len(sections) != 1 or sections[0] != section:
        raise Exception("Config input must have only one section with the "
                        "same name as the specified actions")
    return config.items(section)

def main(args):
    actions = {
      'inverseIsing':   inverseIsing,
      'getEnergies':    getEnergies,
      'benchmark':      MCMCbenchmark,
      #'measureFPerror': measureFPerror,
      'subseqFreq':     subseqFreq,
      'mcmc':           equilibrate,
      'equil_pt':       equil_PT,
      'nestedZ':        nestedZ,
      'test':           testing,
     }

    descr = 'Perform biophysical Potts Model calculations on the GPU'
    parser = argparse.ArgumentParser(description=descr, add_help=False)
    add = parser.add_argument
    add('action', choices=actions.keys(), nargs='?', default=None,
        help="Computation to run")
    add('--clinfo', action=CLInfoAction, help="Display detected GPUs")
    add('-h', '--help', action='store_true',
        help="show this help message and exit")


    known_args, remaining_args = parser.parse_known_args(args)

    if known_args.action is None:
        if known_args.help:
            print(parser.format_help())
            return
        print(parser.format_usage())
        return

    if known_args.help:
        remaining_args.append('-h')

    #if not sys.stdin.isatty(): #config file supplied in stdin
    #    config = readConfig(sys.stdin, known_args.action)
    #    configargs = [arg for opt,val in config for arg in ('--'+opt, val)]
    #    remaining_args = configargs + remaining_args

    actions[known_args.action](remaining_args, print)

if __name__ == '__main__':
    main(sys.argv[1:])