import numpy as np
import pyle
from labrad.units import Unit
from matplotlib import mlab
from matplotlib import pyplot as plt
from pyle import envelopes as env
from pyle import gateCompiler as gc
from pyle import gates
from pyle.analysis import readout
from pyle.dataking import sweeps
from pyle.dataking import util
from pyle.dataking import zfuncs
from pyle.dataking.fpgaseqTransmonV7 import runQubits
from pyle.dataking.singleQubitTransmon import calculateZpaFunc
from pyle.envelopes import NumericalPulse, Envelope
from pyle.fitting import fitting
from pyle.gates import Gate
from pyle.pipeline import returnValue
from pyle.util import convertUnits
from pyle.util import sweeptools as st
from scipy.optimize import curve_fit
from scipy.interpolate import interp1d
import math
from scipy.integrate import quad
from pyle.plotting import dstools
from pyle.plotting import tomography as tg
from pyle import tomo
from pyle.analysis.stateTomography import correctVisibility
from pyle.pipeline import returnValue
from pyle.pipeline import FutureList
from pyle.math import ket2rho, fidelity
from pyle.datavault import DataVaultWrapper
from pyle.plotting.tomography import plotTrajectory
from pyle.math import dot3
from pyle.dataking import singleQubitTransmon as sq
# COLORS
BLUE   = "#348ABD"
RED    = "#E24A33"
PURPLE = "#988ED5"
YELLOW = "#FBC15E"
GREEN  = "#8EBA42"
PINK   = "#FFB5B8"
GRAY   = "#777777"
COLORS = [BLUE, RED, GREEN, YELLOW, PURPLE, PINK, GRAY]

V, mV, us, ns, GHz, MHz, dBm, rad, au = [Unit(s) for s in
                                         ('V', 'mV', 'us', 'ns', 'GHz', 'MHz', 'dBm', 'rad', 'au')]

opLabel = lambda ops: ','.join(op[0] for op in ops)
stateLabel = lambda state: bin(state)[2:].rjust(1, '0')
Uhardmard = np.array([[1,1],[1,-1]])*np.sqrt(0.5)

basis = {'I': tomo.sigmaI, 'X': tomo.Xpi, 'Y': tomo.Ypi,
         'Z': tomo.Zpi, 'X/2': tomo.Xpi2, 'Y/2': tomo.Ypi2,
         'Z/2': tomo.Zpi2, '-X': tomo.Xmpi, '-Y': tomo.Ympi,
         '-Z': tomo.Zmpi, '-X/2': tomo.Xmpi2, '-Y/2': tomo.Ympi2,
         '-Z/2': tomo.Zmpi2}
basis1 = {'I': tomo.sigmaI, 'X': tomo.Xpi, 'Y': tomo.Ypi,
         'Z': tomo.Zpi, 'X/2': tomo.Xpi2, 'Y/2': tomo.Ypi2,
         'Z/2': tomo.Zpi2, '-X': tomo.Xmpi, '-Y': tomo.Ympi,
         '-Z': tomo.Zmpi, '-X/2': tomo.Xmpi2, '-Y/2': tomo.Ympi2,
         '-Z/2': tomo.Zmpi2,'H':Uhardmard,'Z/4':tomo.Rmat(tomo.sigmaZ,np.pi/4)}

Gatelist={'SE': lambda q: gates.Echo([q], q['identityWaitLen']),
'I': lambda q: gates.Wait([q], 0 * ns),
'X': lambda q: gates.PiPulse([q]),
'Y': lambda q: gates.PiPulse([q], phase=np.pi/2.),
'X/2': lambda q: gates.PiHalfPulse([q]),
'Y/2': lambda q: gates.PiHalfPulse([q], phase=np.pi/2.),
'-X': lambda q: gates.PiPulse([q], phase=np.pi),
'-Y': lambda q: gates.PiPulse([q], phase=3 * np.pi/2.),
'-X/2': lambda q: gates.PiHalfPulse([q], phase=np.pi),
'-Y/2': lambda q: gates.PiHalfPulse([q], phase=3*np.pi/2.),
'H': lambda q: gates.FastRFHadamard([q]),
'D': lambda q: gates.Detune([q]),
'Z': lambda q: gates.PiPulseZ([q]),
'Z/2': lambda q: gates.PiHalfPulseZ([q])}


def prestate(state='I', level=2):
    state0 = np.array([[1], [0]])
    rho = ket2rho(np.dot(basis[state], state0))

    if level == 2:
        return rho
    else:
        rho3 = np.zeros((3, 3), dtype='complex')
        rho3[0:2, 0:2] = rho[0:2, 0:2]
    return rho3


def set_init_state(state=['I', 'X']):
    state1 = prestate(state=state[0])
    state2 = prestate(state=state[1])
    return np.kron(state1, state2)

def fMatrix(q):
    qubit = q
    s10 = qubit['calReadoutFids'][0]
    s11 = qubit['calReadoutFids'][1]
    S = np.array([[s10, 1 - s11], [1-s10, s11]])
    return S

def measfMatrix(q0, q1):
    sMatrices = []
    for qubit in [q0, q1]:
        f0 = qubit['calReadoutFids'][0]
        f1 = qubit['calReadoutFids'][1]
        fM = np.array([[f0, 1 - f1], [1 - f0, f1]])
        sMatrices.append(fM)
    return reduce(np.kron, sMatrices)


class testCzpulse(NumericalPulse):
    "Czpulse with the similar Form of fast Cz, but using the STA and rescale func"

    NP_padTime = 4000 * ns
    @convertUnits(t0='ns', T='ns', G='GHz')
    def __init__(self, q1, q2, t0=0.0 * ns, T=20.0 * ns, G=0.01 * 2 * np.sqrt(2) * MHz, thetaf=np.pi / 3,
                 N=20001,
                 back=False):
        self.q1 = q1
        self.q2 = q2
        self.t0 = t0
        self.T = T
        self.G = G
        self.thetaf = thetaf
        self.nonlin = q2['f21']['GHz'] - q2['f10']['GHz']
        self.detuning = zfuncs.AmpToFrequency(self.q1)(0) - q1['Targetfreq']['GHz']
        # self.detuning = q1['f10']['GHz'] - q1['Targetfreq']['GHz']
        self.N = N
        self.back = back
        Envelope.__init__(self, start=t0, end=t0 + 2 * T)

    def timeFunc(self, t):
        # Bx~constant dtheta/dt~lam*(1-cos(2pi*t/T))
        tp = np.linspace(self.t0, self.t0 + self.T, self.N)
        # thetai = arctan(G/(detuning+nonlin))
        thetai = np.arctan(self.G / self.detuning)
        lam = (self.thetaf - thetai) / self.T
        # theta
        theta = lam * ((tp - self.t0) - self.T / 2 / np.pi * np.sin(
            2 * np.pi / self.T * (tp - self.t0))) + thetai
        # By = -dtheta/dt/2/np.pi
        By = -lam / 2 / np.pi * (1 - np.cos(2 * np.pi / self.T * (tp - self.t0)))
        Bx = self.G
        # with the transformation, dphi/dt is applied in Z direction
        phi = np.arctan2(By, Bx)
        dphi = (phi[1:self.N] - phi[0:self.N - 1]) / (tp[1] - tp[0]) / 2 / np.pi
        dphi = interp1d(tp[0:self.N - 1], dphi, bounds_error=False, fill_value=0)
        Bz = self.G / np.tan(theta) + dphi(tp)
        # rescale Z= G*Bz/sqrt(By**2+Bx**2)
        z = self.G * Bz / np.sqrt(By ** 2 + Bx ** 2)
        # rescale evolution time tau = cumSum(sqrt(By**2+Bx**2))*deltat+t0
        tau = np.cumsum(np.sqrt(By ** 2 + Bx ** 2) / self.G) * self.T / self.N + self.t0
        if self.back:
            secondT = np.sqrt(By ** 2 + Bx ** 2) / self.G * self.T / self.N
            secondT = np.cumsum(secondT[::-1]) + max(tau)
            tau = np.hstack((tau, secondT))
            z = np.hstack((z, z[::-1]))
        z = interp1d(tau, z, bounds_error=False, fill_value=z[0])
        # adjustz = z(t) + self.q2['f10']['GHz']-self.nonlin
        adjustz =(z(t) + self.q1['Targetfreq']['GHz'])
        func = zfuncs.FrequencyToAmp(self.q1)
        return (func(adjustz))*(t>0)*(t<max(tau))#(func(adjustz)-func(self.q1['f10']['GHz']))*(t>0)*(t<max(tau))


class testCZ_gate(Gate):
    def __init__(self, agents, length=None, G=None, phase0=None, phase1=None, tbuf=8.0 * ns, thetaf=None, amp=0.00):
        """
        @param agents: agents, the gate is applied to the first element, agents[0]
        @param G: the coupling strength between |11> and |02>
        """
        self.length = length
        if self.length == None:
            self.length = agents[0]['testCzlen']
        self.T = 2 * self.length['ns']
        self.tbuf = tbuf
        self.phase0 = phase0
        self.phase1 = phase1
        self.G = G
        self.amp = amp
        if G == None:
            self.G = agents[0]['Czstrength']
        # if thetaf==None:
        #     self.thetaf = self.piphase_para()
        # else:
        #     self.thetaf = agents[0]['Czthetaf']
        if thetaf==None:
            self.thetaf = agents[0]['Czthetaf']
        else:
            self.thetaf =  thetaf#self.piphase_para()
        print 'theta final is ', self.thetaf
        # print 'theory theta is ' ,self.piphase_para()
        if phase0 == None:
            self.phase0 = agents[0]['testCzphase']
        if phase1 == None:
            self.phase1 = agents[1]['testCzphase']
        if math.isnan(self.thetaf):
            raise Exception("need more evolution time!!")
        Gate.__init__(self, agents)

    def updateAgents(self):
        q1 = self.agents[0]
        q2 = self.agents[1]
        t = q1['_t']
        if self.length == None:
            l = q1['testCzlen']
        else:
            l = self.length
        q1['z'] += testCzpulse(q1, q2, t0=t, T=self.length, G=self.G, thetaf=self.thetaf)
        q1['_t'] += 2 * l + 2 * self.tbuf+30*ns
        q1['xy_phase'] += self.phase0
        q2['xy_phase'] += self.phase1

    def _name(self):
        return "test Cz gate"

    def phase_accumulate(self, thetaf=np.pi / 3, T=20.0, G=0.02 * np.sqrt(2)):
        y = lambda x: 2 * np.pi * G * np.tan(thetaf / T * (x - T / 2 / np.pi * np.sin(2 * np.pi / T * x)) / 2)
        return quad(y, 0, T)[0]

    def piphase_para(self, phase_ac=np.pi):
        theta = np.linspace(1.0 / 8.0, 1.0, 101) * np.pi
        phase = []
        [phase.append(self.phase_accumulate(thetaf, T=self.T / 2, G=self.G['GHz'])) for thetaf in theta]
        phase_c = interp1d(phase, theta, bounds_error=False, fill_value=None)
        return phase_c(phase_ac)

    def rescale_T(self, t0=0.0, N=20001):
        # Bx~constant dtheta/dt~lam*(1-cos(2pi*t/T))
        q1 = self.agents[0]
        q2 = self.agents[1]
        # parameters
        # detuning = q1['f10']['GHz']-q2['f10']['GHz']
        # nonlin = q2['f21']['GHz'] - q2['f10']['GHz']
        #
        detuning = q1['f10']['GHz'] - q1['Targetfreq']['GHz']
        tp = np.linspace(t0, t0 + self.T, N)
        # thetai = np.arctan(self.G['GHz'] / (detuning + nonlin))
        thetai = np.arctan(self.G['GHz'] / detuning)
        lam = (self.thetaf - thetai) / self.T
        By = lam / 2 / np.pi * (1 - np.cos(2 * np.pi / self.T * (tp - t0)))
        Bx = self.G['GHz']
        tau = np.cumsum(np.sqrt(By ** 2 + Bx ** 2) / self.G['GHz']) * self.T / N
        return 2 * max(tau)



class SwapCZ(Gate):
    def __init__(self, agents, tlen=None, amp=None, phase0=None, phase1=None, overshoot=0.0, overshoot_w=1.0):
        self.tlen = tlen
        self.amp = amp
        self.phase0 = phase0
        self.phase1 = phase1
        self.overshoot = overshoot
        self.overshoot_w = overshoot_w
        if tlen == None:
            self.tlen = 2*agents[0]['Swapczlen']
        if amp == None:
            self.amp = agents[0]['Swapczamp']
        if phase0 == None:
            self.phase0 = agents[0]['testCzphase']
        if phase1 == None:
            self.phase1 = agents[1]['testCzphase']
        Gate.__init__(self, agents)

    def updateAgents(self):
        q0 = self.agents[0]
        q1 = self.agents[1]
        t = q0['_t']
        tlen = self.tlen
        amp = self.amp
        if self.tlen == None:
            l = 2*q0['Swapczlen']
        else:
            l = self.tlen
        q0['z'] += env.rect(t, tlen, amp, overshoot=self.overshoot,overshoot_w=self.overshoot_w)
        q0['_t'] += l+10*ns
        q0['xy_phase'] += self.phase0
        q1['xy_phase'] += self.phase1

    def _name(self):
        return "SwapCZ"


def test_SwapCz_Pop(Sample, phase, measure=(0, 1), control=True, stats=1200, tBuf=5 * ns,
                name='test SwapCz control', save=True, noisy=True):
    sample, devs, qubits = gc.loadQubits(Sample, measure=measure)

    axes = [(phase, 'phase compensate')]

    deps = [("|00>", '', ''), ("|01>", '', ''),
            ("|10>", '', ''), ("|11>", '', '')]
    kw = {"stats": stats, 'tBuf': tBuf, 'Control': control}

    dataset = sweeps.prepDataset(sample, name, axes, deps, measure=measure, kw=kw)

    def func(server, currphase):
        alg = gc.Algorithm(devs)
        q0 = alg.q0
        q1 = alg.q1
        alg[gates.PiHalfPulse([q0])]
        if control:
            alg[gates.PiPulse([q1])]
        alg[gates.Wait([q0], 2 * q0['piLen'])]
        #alg[testCZ_gate([q0, q1], length=q0['testCzlen'], G=q0['Czstrength'], phase0=currphase)]
        alg[SwapCZ([q0,q1], tlen=q0['Swapczlen']*2, amp=q0['Swapczamp'], phase0=currphase)]
        alg[gates.Wait([q0], tBuf)]
        alg[gates.PiHalfPulse([q0])]
        if control:
            alg[gates.Sync([q0, q1])]
            alg[gates.PiPulse([q1])]
        alg[gates.Measure([q0, q1])]
        alg.compile(correctXtalkZ=True, config=['q3', 'q4'])
        data = yield runQubits(server, alg.agents, stats, dataFormat='iqRaw')
        probs = np.squeeze(
            readout.iqToProbs(data, alg.qubits, states=[0, 1], correlated=True)).flat
        returnValue([probs[0], probs[1], probs[2], probs[3]])

    data = sweeps.grid(func, axes, dataset=dataset, save=save, noisy=noisy)
    return data

def pop2(Sample, measure=(0,1), currlen=st.r[25:35:0.2, ns], curramp=st.r[-0.19:-0.18:0.0005], control=True, stats=300L, tBuf=5*ns, name='pop2', save=True, noisy=True):

    sample, devs, qubits = gc.loadQubits(Sample, measure=measure)

    axes = [(currlen, 'swaplength'),(curramp, 'Amp')]
    qNames = [dev.__name__ for dev in devs if dev.get("readout", False)]

    deps = [("|00>", '', ''), ("|01>", '', ''),("|02>", '', ''),
            ("|10>", '', ''), ("|11>", '', ''),("|12>", '', ''),
            ("|20>", '', ''), ("|21>", '', ''),("|22>", '', '')]
    kw = {"stats": stats, 'tBuf': tBuf}

    dataset = sweeps.prepDataset(sample, name, axes, deps, measure=measure, kw=kw)

    def func(server, currlen, curramp):
        alg = gc.Algorithm(devs)
        q0 = alg.q0
        q1 = alg.q1
        alg[gates.PiPulse([q0])]
        if control:
            alg[gates.PiPulse([q1])]
        alg[gates.Wait([q0], tBuf)]
        # alg[testCZ_gate([q0, q1], length=currdelay, G=q0['Czstrength'],thetaf=currphase)]
        alg[gates.Detune([q0], tlen=currlen*2, amp=curramp)]
        alg[gates.Wait([q0], tBuf)]
        # alg[gates.PiPulse([q0])]
        if control:
            alg[gates.Sync([q0, q1])]
            alg[gates.PiPulse([q1])]
        alg[gates.Wait([q0], tBuf)]
        alg[gates.Measure([q0, q1])]
        alg.compile(correctXtalkZ=True, config=['q3', 'q4'])
        data = yield runQubits(server, alg.agents, stats, dataFormat='iqRaw')
        probs = np.squeeze(
            readout.iqToProbs(data, alg.qubits, states=[0, 1, 2], correlated=True)).flat
        returnValue([probs[0], probs[1],probs[2], probs[3],
            probs[4], probs[5],probs[6], probs[7],probs[8]])

    data = sweeps.grid(func, axes, dataset=dataset, save=save, noisy=noisy)

    return data

def test_Cz_ramsey2(Sample, swaplen=st.r[26:35:0.2], delay=st.r[0:500:2,ns], measure=(0, 1), control=True, stats=1200, tBuf=0 * ns, name='test Cz phase_confirm_2', save=True, noisy=True, fringeFreq=10*MHz ):
    sample, devs, qubits = gc.loadQubits(Sample, measure=measure)

    axes = [(swaplen, 'swaplen [ns]'),(delay, 'delay [ns]')]

    deps = [("|1> control off", '', ''),
            ("|1> control on", '', '')]
    kw = {"stats": stats, 'tBuf': tBuf, 'Control': control, 'fringeFreq':fringeFreq }

    dataset = sweeps.prepDataset(sample, name, axes, deps, measure=measure, kw=kw)

    def func(server, currlen, currDelay):

        P = []
        for control in [False, True]:
            alg = gc.Algorithm(devs)
            q0 = alg.q0
            q1 = alg.q1
            alg[gates.PiHalfPulse([q0])]
            if control:
                alg[gates.PiPulse([q1])]
            alg[gates.Sync([q0, q1])]
            alg[gates.Wait([q0], tBuf)]
            # alg[testCZ_gate([q0, q1], length=q0['testCzlen'], G=q0['Czstrength'])]
            alg[gates.Detune([q0], tlen=currlen*2*ns, amp=0.001837*currlen-0.245935)]
            alg[gates.Wait([q0], tBuf)]
            alg[gates.Sync([q0, q1])]
            currphase = float(fringeFreq*currDelay)*2*np.pi
            alg[gates.Wait([q0], currDelay)]
            alg[gates.PiHalfPulse([q0],phase=currphase)]
            if control:
                alg[gates.PiPulse([q1])]
            alg[gates.Measure([q0, q1])]
            alg.compile(correctXtalkZ=True, config=['q3', 'q4'])
            data = yield runQubits(server, alg.agents, stats, dataFormat='iqRaw')
            probs = np.squeeze(readout.iqToProbs(data, alg.qubits, states=[0, 1], correlated=False)).flatten()
            P.append(probs[1])
        returnValue(P)

    data = sweeps.grid(func, axes, dataset=dataset, save=save, noisy=noisy)
    return data


def test_Cz_controlphase(Sample, g, phase=np.linspace(0, 2 * np.pi, 100), measure=(0, 1), stats=1200,
                         tBuf=20 * ns,
                         name='test Cz control', save=True, noisy=True):
    sample, devs, qubits = gc.loadQubits(Sample, measure=measure)

    axes = [(phase, 'phase compensate'),(g,'coupling strength')]

    deps = [("|1> control off", '', ''),
            ("|1> control on", '', '')]
    kw = {"stats": stats, 'tBuf': tBuf}

    dataset = sweeps.prepDataset(sample, name, axes, deps, measure=measure, kw=kw)

    def func(server, currphase, currg):

        P = []
        for control in [False, True]:
            alg = gc.Algorithm(devs)
            q0 = alg.q0
            q1 = alg.q1
            alg[gates.PiHalfPulse([q1])]
            if control:
                alg[gates.PiPulse([q0])]
            alg[gates.Sync([q0, q1])]
            alg[gates.Wait([q1], tBuf)]
            alg[gates.Sync([q0, q1])]
            alg[testCZ_gate([q0, q1], length=q0['testCzlen'], G=currg, phase1=currphase)]
            alg[gates.Sync([q0, q1])]
            alg[gates.Wait([q1], tBuf)]
            alg[gates.Sync([q0, q1])]
            alg[gates.PiHalfPulse([q1])]
            if control:
                alg[gates.PiPulse([q0])]
            alg[gates.Measure([q0, q1])]
            alg.compile(correctXtalkZ=True, config=['q3', 'q4'])
            data = yield runQubits(server, alg.agents, stats, dataFormat='iqRaw')
            probs = np.squeeze(readout.iqToProbs(data, alg.qubits, states=[0, 1], correlated=False)).flatten()
            P.append(probs[3])
        returnValue(P)

    data = sweeps.grid(func, axes, dataset=dataset, save=save, noisy=noisy)
    return data


def test_Cz_controlphase_confirm(Sample, phase=np.linspace(0, 2 * np.pi, 100), measure=(0, 1), stats=1200,
                         tBuf=20 * ns,
                         name='test Cz control', save=True, noisy=True, repeatNum=3):
    sample, devs, qubits = gc.loadQubits(Sample, measure=measure)

    axes = [(phase, 'phase compensate')]

    deps = [("|1> control off", '', ''),
            ("|1> control on", '', '')]
    kw = {"stats": stats, 'tBuf': tBuf}

    dataset = sweeps.prepDataset(sample, name, axes, deps, measure=measure, kw=kw)

    def func(server, currphase):

        P = []
        for control in [False, True]:
            alg = gc.Algorithm(devs)
            q0 = alg.q0
            q1 = alg.q1
            alg[gates.PiHalfPulse([q1])]
            if control:
                alg[gates.PiPulse([q0])]
            alg[gates.Sync([q0, q1])]
            alg[gates.Wait([q1], tBuf)]
            alg[gates.Sync([q0, q1])]
            for i in range(repeatNum):
                alg[testCZ_gate([q0, q1], length=q0['testCzlen'], G=q0['Czstrength'])]
            alg[gates.Sync([q0, q1])]
            alg[gates.Wait([q1], tBuf)]
            alg[gates.Sync([q0, q1])]
            alg[gates.PiHalfPulse([q1],phase=currphase)]
            if control:
                alg[gates.PiPulse([q0])]
            alg[gates.Measure([q0, q1])]
            alg.compile(correctXtalkZ=True, config=['q3', 'q4'])
            data = yield runQubits(server, alg.agents, stats, dataFormat='iqRaw')
            probs = np.squeeze(readout.iqToProbs(data, alg.qubits, states=[0, 1], correlated=False)).flatten()
            P.append(probs[3])
        returnValue(P)

    data = sweeps.grid(func, axes, dataset=dataset, save=save, noisy=noisy)
    return data


def test_Cz_controlbit_Ztal(Sample, delay=st.r[0:500:10,ns], measure=(0, 1), stats=1200,
                         tBuf=20 * ns,
                         name='test Cz control', save=True, noisy=True, repeatNum=3, control=True, fringefreq=10*MHz):
    sample, devs, qubits = gc.loadQubits(Sample, measure=measure)

    axes = [(delay, 'delay')]

    deps = [("|1> control off", '', '')]
    kw = {"stats": stats, 'tBuf': tBuf}

    dataset = sweeps.prepDataset(sample, name, axes, deps, measure=measure, kw=kw)

    def func(server, currdelay):

        P = []

        alg = gc.Algorithm(devs)
        q0 = alg.q1
        q1 = alg.q0
        if control:
            alg[gates.PiPulse([q0])]
            alg[gates.Sync([q0, q1])]
        alg[gates.PiHalfPulse([q1])]
        alg[gates.Sync([q0, q1])]
        # alg[gates.Wait([q1], tBuf)]
        # alg[gates.Sync([q0, q1])]
        # for i in range(repeatNum):
        #     alg[testCZ_gate([q0, q1], length=q0['testCzlen'], G=q0['Czstrength'])]
            # alg[gates.Detune([q0],60*ns, 0.3)]
        # alg[gates.Sync([q0, q1])]
        alg[gates.Wait([q1], currdelay)]
        alg[gates.Sync([q0, q1])]
        phae = 2*np.pi*fringefreq*currdelay
        alg[gates.PiHalfPulse([q1],phase=phae)]
        alg[gates.Measure([q0, q1])]
        alg.compile(correctXtalkZ=True, config=['q3', 'q4'])
        data = yield runQubits(server, alg.agents, stats, dataFormat='iqRaw')
        probs = np.squeeze(readout.iqToProbs(data, alg.qubits, states=[0, 1], correlated=False)).flatten()
        P.append(probs[measure[0]*2+1])
        returnValue(P)

    data = sweeps.grid(func, axes, dataset=dataset, save=save, noisy=noisy)
    return data

def test_Cz_targetbit_Ztal(Sample, delay=np.linspace(0, 2 * np.pi, 100), measure=(0, 1), stats=1200,
                         tBuf=20 * ns,
                         name='test Cz Ztal', save=True, noisy=True, repeatNum=3, tdelay=20*ns):
    sample, devs, qubits = gc.loadQubits(Sample, measure=measure)

    axes = [(delay, 'delay')]

    deps = [("|1> control off", '', '')]
    kw = {"stats": stats, 'tBuf': tBuf}

    dataset = sweeps.prepDataset(sample, name, axes, deps, measure=measure, kw=kw)

    def func(server, currdelay):

        P = []
        for control in [False]:
            alg = gc.Algorithm(devs)
            q0 = alg.q0
            q1 = alg.q1
            alg[gates.PiHalfPulse([q0])]
            alg[gates.Sync([q0, q1])]
            alg[gates.Wait([q1], tBuf)]
            alg[gates.Sync([q0, q1])]
            for i in range(repeatNum):

                alg[testCZ_gate([q0, q1], length=q0['testCzlen'], G=q0['Czstrength'])]
                alg[gates.Wait([q0], tdelay)]
                # alg[gates.Detune([q0],60*ns, -0.5)]
            alg[gates.Sync([q0, q1])]
            alg[gates.Wait([q0], currdelay)]
            alg[gates.Sync([q0, q1])]
            alg[gates.PiHalfPulse([q0],phase=np.pi/2)]
            alg[gates.Measure([q0, q1])]
            alg.compile(correctXtalkZ=True, config=['q3', 'q4'])
            data = yield runQubits(server, alg.agents, stats, dataFormat='iqRaw')
            probs = np.squeeze(readout.iqToProbs(data, alg.qubits, states=[0, 1], correlated=False)).flatten()
            P.append(probs[1])
        returnValue(P)

    data = sweeps.grid(func, axes, dataset=dataset, save=save, noisy=noisy)
    return data


def test_Cz_tunnphase_confirm(Sample, phase, g, measure=(0, 1), control=True, stats=1200, tBuf=20 * ns,
                     name='test Cz phase_confirm', save=True, noisy=True, repeatNum=1):
    sample, devs, qubits = gc.loadQubits(Sample, measure=measure)

    axes = [(phase, 'phase compensate'), (g, 'coupling')]

    deps = [("|1> control off", '', ''),
            ("|1> control on", '', '')]
    kw = {"stats": stats, 'tBuf': tBuf, 'Control': control, 'repeatNum':repeatNum}

    dataset = sweeps.prepDataset(sample, name, axes, deps, measure=measure, kw=kw)

    def func(server, currphase, currg):

        P = []
        for control in [False, True]:
            alg = gc.Algorithm(devs)
            q0 = alg.q0
            q1 = alg.q1
            alg[gates.PiHalfPulse([q0])]
            if control:
                alg[gates.PiPulse([q1])]
            alg[gates.Sync([q0, q1])]
            alg[gates.Wait([q0], tBuf)]
            for i in range(repeatNum):
                alg[testCZ_gate([q0, q1], length=q0['testCzlen'], G=currg, phase0=None)]
            alg[gates.Wait([q0], tBuf)]
            alg[gates.Sync([q0, q1])]
            alg[gates.PiHalfPulse([q0],phase=currphase)]
            if control:
                alg[gates.PiPulse([q1])]
            alg[gates.Measure([q0, q1])]
            alg.compile(correctXtalkZ=True, config=['q3', 'q4'])
            data = yield runQubits(server, alg.agents, stats, dataFormat='iqRaw')
            probs = np.squeeze(readout.iqToProbs(data, alg.qubits, states=[0, 1], correlated=False)).flatten()
            P.append(probs[1])
        returnValue(P)

    data = sweeps.grid(func, axes, dataset=dataset, save=save, noisy=noisy)
    return data


def test_Cz_ramsey(Sample, delay=st.r[0:500:2,ns], measure=(0, 1), control=True, stats=1200, tBuf=20 * ns,
                     name='test Cz phase_confirm', save=True, noisy=True, fringeFreq=10*MHz ):
    sample, devs, qubits = gc.loadQubits(Sample, measure=measure)

    axes = [(delay, 'delay [ns]')]

    deps = [("|1> control off", '', ''),
            ("|1> control on", '', '')]
    kw = {"stats": stats, 'tBuf': tBuf, 'Control': control, 'fringeFreq':fringeFreq }

    dataset = sweeps.prepDataset(sample, name, axes, deps, measure=measure, kw=kw)

    def func(server, currDelay):

        P = []
        for control in [False, True]:
            alg = gc.Algorithm(devs)
            q0 = alg.q0
            q1 = alg.q1
            alg[gates.PiHalfPulse([q0])]
            if control:
                alg[gates.PiPulse([q1])]
            alg[gates.Sync([q0, q1])]
            alg[gates.Wait([q0], tBuf)]
            alg[testCZ_gate([q0, q1], length=q0['testCzlen'], G=q0['Czstrength'])]
            alg[gates.Wait([q0], tBuf)]
            alg[gates.Sync([q0, q1])]
            currphase = float(fringeFreq*currDelay)*2*np.pi
            alg[gates.Wait([q0], currDelay)]
            alg[gates.PiHalfPulse([q0],phase=currphase)]
            if control:
                alg[gates.PiPulse([q1])]
            alg[gates.Measure([q0, q1])]
            alg.compile(correctXtalkZ=True, config=['q3', 'q4'])
            data = yield runQubits(server, alg.agents, stats, dataFormat='iqRaw')
            probs = np.squeeze(readout.iqToProbs(data, alg.qubits, states=[0, 1], correlated=False)).flatten()
            P.append(probs[1])
        returnValue(P)

    data = sweeps.grid(func, axes, dataset=dataset, save=save, noisy=noisy)
    return data

def test_Cz_ramsey1(Sample, delay=st.r[0:500:2,ns], measure=(0, 1), control=True, stats=1200, tBuf=20 * ns,
                     name='test Cz phase_confirm_1', save=True, noisy=True, fringeFreq=10*MHz ):
    sample, devs, qubits = gc.loadQubits(Sample, measure=measure)

    axes = [(delay, 'delay [ns]')]

    deps = [("|1> control off", '', ''),
            ("|1> control on", '', '')]
    kw = {"stats": stats, 'tBuf': tBuf, 'Control': control, 'fringeFreq':fringeFreq }

    dataset = sweeps.prepDataset(sample, name, axes, deps, measure=measure, kw=kw)

    def func(server, currDelay):

        P = []
        for control in [False, True]:
            alg = gc.Algorithm(devs)
            q0 = alg.q0
            q1 = alg.q1
            alg[gates.PiHalfPulse([q0])]
            if control:
                alg[gates.PiPulse([q1])]
            alg[gates.Sync([q0, q1])]
            alg[gates.Wait([q0], tBuf)]
            # alg[testCZ_gate([q0, q1], length=q0['testCzlen'], G=q0['Czstrength'])]
            #alg[gates.Detune([q0], tlen=q0['Swapczlen']*2, amp=q0['Swapczamp'])]
            alg[SwapCZ([q0,q1], tlen=q0['Swapczlen']*2, amp=q0['Swapczamp'])]
            alg[gates.Wait([q0], tBuf)]
            alg[gates.Sync([q0, q1])]
            currphase = float(fringeFreq*currDelay)*2*np.pi
            alg[gates.Wait([q0], currDelay)]
            alg[gates.PiHalfPulse([q0],phase=currphase)]
            if control:
                alg[gates.PiPulse([q1])]
            alg[gates.Measure([q0, q1])]
            alg.compile(correctXtalkZ=True, config=['q3', 'q4'])
            data = yield runQubits(server, alg.agents, stats, dataFormat='iqRaw')
            probs = np.squeeze(readout.iqToProbs(data, alg.qubits, states=[0, 1], correlated=False)).flatten()
            P.append(probs[1])
        returnValue(P)

    data = sweeps.grid(func, axes, dataset=dataset, save=save, noisy=noisy)
    return data


def test_ramsey_q12(Sample, delay=st.r[0:500:5,ns], measure=(0, 1), stats=1200, tBuf=10 * ns,
                     name='test_ramsey_q1_q2', save=True, noisy=True, fringeFreq=10*MHz ):
    sample, devs, qubits = gc.loadQubits(Sample, measure=measure)

    axes = [(delay, 'delay [ns]')]

    deps = [("|00>", '', ''), ("|01>", '', ''),
            ("|10>", '', ''), ("|11>", '', '')]
    kw = {"stats": stats, 'tBuf': tBuf, 'fringeFreq':fringeFreq }

    dataset = sweeps.prepDataset(sample, name, axes, deps, measure=measure, kw=kw)

    def func(server, currDelay):
        alg = gc.Algorithm(devs)
        q0 = alg.q0
        q1 = alg.q1
        alg[gates.PiHalfPulse([q0])]
        alg[gates.PiHalfPulse([q1])]
        alg[gates.Wait([q0], tBuf)]
        alg[gates.Sync([q0, q1])]
        currphase = float(fringeFreq*currDelay)*2*np.pi
        alg[gates.Wait([q0], currDelay)]
        alg[gates.Wait([q1], currDelay)]
        alg[gates.PiHalfPulse([q0],phase=currphase)]
        alg[gates.PiHalfPulse([q1],phase=currphase)]
        alg[gates.Measure([q0, q1])]
        alg.compile(correctXtalkZ=False, config=['q3', 'q4'])
        data = yield runQubits(server, alg.agents, stats, dataFormat='iqRaw')
        probs = np.squeeze(
            readout.iqToProbs(data, alg.qubits, states=[0, 1], correlated=True)).flat
        returnValue([probs[0], probs[1], probs[2], probs[3]])

    data = sweeps.grid(func, axes, dataset=dataset, save=save, noisy=noisy)
    return data


def swap11and20(Sample, measure=(0,1), delay=st.r[0:200:2, ns], swapAmp=st.r[-1:1:0.1], stats=300L, prob_correlated=False, tBuf=5*ns, overshoot=0.0, name='swap 11 and 20', save=True, noisy=True):

    sample, devs, qubits = gc.loadQubits(Sample, measure=measure)

    axes = [(swapAmp, 'swap amplitude'), (delay, 'swap time'), (overshoot, 'overshoot')]
    qNames = [dev.__name__ for dev in devs if dev.get("readout", False)]

    deps = [("|00>", '', ''), ("|01>", '', ''),
            ("|10>", '', ''), ("|11>", '', '')]
    kw = {"stats": stats, 'tBuf': tBuf, 'prob_correlated': prob_correlated}

    dataset = sweeps.prepDataset(sample, name, axes, deps, measure=measure, kw=kw)

    def func(server, currAmp, currDelay, currOs):
        alg = gc.Algorithm(devs)
        q0 = alg.q0
        q1 = alg.q1
        alg[gates.PiPulse([q0])]
        alg[gates.PiPulse([q1])]
        alg[gates.Sync([q0, q1])]
        alg[gates.Detune([q0], currDelay, currAmp, currOs)]
        alg[gates.Wait([q0], tBuf)]
        alg[gates.Measure([q0, q1])]
        alg.compile(correctXtalkZ=False, config=['q3', 'q4'])
        data = yield runQubits(server, alg.agents, stats, dataFormat='iqRaw')
        probs = np.squeeze(
            readout.iqToProbs(data, alg.qubits, states=[0, 1], correlated=True)).flat
        returnValue([probs[0], probs[1], probs[2], probs[3]])

    data = sweeps.grid(func, axes, dataset=dataset, save=save, noisy=noisy)

    return data


def Swap11and20(Sample, measure=(0,1), delay=st.r[0:200:2, ns], swapAmp=st.r[-1:1:0.1], stats=300L, prob_correlated=False, tBuf=5*ns, name='Swap 11 and 20', save=True, noisy=True):

    sample, devs, qubits = gc.loadQubits(Sample, measure=measure)

    axes = [(swapAmp, 'swap amplitude'), (delay, 'swap time')]
    qNames = [dev.__name__ for dev in devs if dev.get("readout", False)]

    deps = [("|00>", '', ''), ("|01>", '', ''),("|02>", '', ''),
            ("|10>", '', ''), ("|11>", '', ''),("|12>", '', ''),
            ("|20>", '', ''), ("|21>", '', ''),("|22>", '', '')]
    kw = {"stats": stats, 'tBuf': tBuf, 'prob_correlated': prob_correlated}

    dataset = sweeps.prepDataset(sample, name, axes, deps, measure=measure, kw=kw)

    def func(server, currAmp, currDelay):
        alg = gc.Algorithm(devs)
        q0 = alg.q0
        q1 = alg.q1
        alg[gates.PiPulse([q0])]
        alg[gates.PiPulse([q1])]
        alg[gates.Sync([q0, q1])]
        alg[gates.Detune([q0], currDelay, currAmp)]
        alg[gates.Wait([q0], tBuf)]
        alg[gates.Measure([q0, q1])]
        alg.compile(correctXtalkZ=False, config=['q3', 'q4'])
        data = yield runQubits(server, alg.agents, stats, dataFormat='iqRaw')
        probs = np.squeeze(
            readout.iqToProbs(data, alg.qubits, states=[0, 1, 2], correlated=True)).flat
        returnValue([probs[0], probs[1],probs[2], probs[3],
            probs[4], probs[5],probs[6], probs[7],probs[8]])

    data = sweeps.grid(func, axes, dataset=dataset, save=save, noisy=noisy)

    return data


def test_Cz_tunnCoup(Sample, phase, g, measure=(0, 1), control=True, stats=1200, tBuf=20 * ns,
                     name='test Cz control', save=True, noisy=True, repeatNum=5, amp=0.02):
    sample, devs, qubits = gc.loadQubits(Sample, measure=measure)

    axes = [(phase, 'phase compensate'), (g, 'coupling')]

    deps = [("|1> control off", '', ''),
            ("|1> control on", '', '')]
    kw = {"stats": stats, 'tBuf': tBuf, 'Control': control, 'CZ pulse number':repeatNum}

    dataset = sweeps.prepDataset(sample, name, axes, deps, measure=measure, kw=kw)

    def func(server, currphase, currg):

        P = []
        for control in [False, True]:
            alg = gc.Algorithm(devs)
            q0 = alg.q0
            q1 = alg.q1
            alg[gates.PiHalfPulse([q0])]
            if control:
                alg[gates.PiPulse([q1])]
            alg[gates.Sync([q0, q1])]
            alg[gates.Wait([q0], tBuf)]
            for i in range(repeatNum):
                alg[gates.Wait([q0], tBuf)]
                alg[testCZ_gate([q0, q1], length=q0['testCzlen'], G=currg, phase0=currphase, thetaf=None, amp=amp)]
            alg[gates.Wait([q0], tBuf)]
            alg[gates.Sync([q0, q1])]
            alg[gates.PiHalfPulse([q0],phase=0.0)]
            if control:
                alg[gates.PiPulse([q1])]
            alg[gates.Measure([q0, q1])]
            alg.compile(correctXtalkZ=True, config=['q3', 'q4'])
            data = yield runQubits(server, alg.agents, stats, dataFormat='iqRaw')
            probs = np.squeeze(readout.iqToProbs(data, alg.qubits, states=[0, 1], correlated=False)).flatten()
            P.append(probs[1])
        returnValue(P)

    data = sweeps.grid(func, axes, dataset=dataset, save=save, noisy=noisy)
    return data


def test_Cz_tunntheta(Sample, phase, measure=(0, 1), control=True, stats=1200, tBuf=20 * ns,
                     name='test Cz control', save=True, noisy=True, repeatNum=5, amp=0.02):
    sample, devs, qubits = gc.loadQubits(Sample, measure=measure)

    axes = [(phase, 'phase compensate')]

    deps = [("|1> control off", '', ''),
            ("|1> control on", '', '')]
    kw = {"stats": stats, 'tBuf': tBuf, 'Control': control, 'CZ pulse number':repeatNum}

    dataset = sweeps.prepDataset(sample, name, axes, deps, measure=measure, kw=kw)

    def func(server, currphase):

        P = []
        for control in [False, True]:
            alg = gc.Algorithm(devs)
            q0 = alg.q0
            q1 = alg.q1
            alg[gates.PiHalfPulse([q0])]
            if control:
                alg[gates.PiPulse([q1])]
            alg[gates.Sync([q0, q1])]
            alg[gates.Wait([q0], tBuf)]
            for i in range(repeatNum):
                alg[gates.Wait([q0], tBuf)]
                # alg[testCZ_gate([q0, q1], length=q0['testCzlen'], G=q0['Czstrength'], thetaf=currtheta, amp=amp, phase0=currphase)]
                alg[SwapCZ([q0,q1], tlen=q0['Swapczlen']*2, amp=q0['Swapczamp'], phase0=currphase)]
            alg[gates.Wait([q0], tBuf)]
            alg[gates.Sync([q0, q1])]
            alg[gates.PiHalfPulse([q0],phase=0)]
            if control:
                alg[gates.PiPulse([q1])]
            alg[gates.Measure([q0, q1])]
            alg.compile(correctXtalkZ=True, config=['q3', 'q4'])
            data = yield runQubits(server, alg.agents, stats, dataFormat='iqRaw')
            probs = np.squeeze(readout.iqToProbs(data, alg.qubits, states=[0, 1], correlated=False)).flatten()
            P.append(probs[1])
        returnValue(P)

    data = sweeps.grid(func, axes, dataset=dataset, save=save, noisy=noisy)
    return data



def test_Cz_tunnlength(Sample, phase, length, measure=(0, 1), control=True, stats=1200, tBuf=5 * ns,
                       name='test Cz control', save=True, noisy=True, plot=False):
    sample, devs, qubits = gc.loadQubits(Sample, measure=measure)

    axes = [(phase, 'phase compensate'), (length, 'length')]

    deps = [("|01> control off", '', ''),
            ("|10> control on", '', '')]
    kw = {"stats": stats, 'tBuf': tBuf, 'Control': control}

    dataset = sweeps.prepDataset(sample, name, axes, deps, measure=measure, kw=kw)

    def func(server, currphase, currLen):

        P = []
        for control in [False, True]:
            alg = gc.Algorithm(devs)
            q0 = alg.q0
            q1 = alg.q1
            alg[gates.PiHalfPulse([q0])]
            if control:
                alg[gates.PiPulse([q1])]
            alg[gates.Wait([q0], 2 * q0['piLen'])]
            alg[testCZ_gate([q0, q1], length=currLen, G=q0['Czstrength'], phase0=currphase)]
            alg[gates.Wait([q0], tBuf)]
            alg[gates.PiHalfPulse([q0])]
            if control:
                alg[gates.Sync([q0, q1])]
                alg[gates.PiPulse([q1])]
            alg[gates.Measure([q0, q1])]
            alg.compile(correctXtalkZ=True, config=['q3', 'q2'])
            data = yield runQubits(server, alg.agents, stats, dataFormat='iqRaw')
            probs = np.squeeze(readout.iqToProbs(data, alg.qubits, states=[0, 1], correlated=False)).flatten()
            P.append(probs[1])
        returnValue(P)

    data = sweeps.grid(func, axes, dataset=dataset, save=save, noisy=noisy)
    if plot:
        dataoff = data[:, (0, 1, 2)]
        dataon = data[119][:, (0, 1, 3)]
        phase, g, proboff = dstools.arrange2Dscan(dataoff)
        phase, g, probon = dstools.arrange2Dscan(dataon)
        fitFunc = lambda x, A, phase, off: (A) * np.cos(x + phase) + off
        deltaphase = []
        for i in range(len(g)):
            P1off = proboff[i, :]
            P1on = probon[i, :]
            poff, err = curve_fit(fitFunc, phase, P1off)
            pon, err = curve_fit(fitFunc, phase, P1on)
            deltaphase.append(pon[1] - poff[1])
            plt.figure()
            plt.plot(phase, P1off, 'o')
            plt.plot(phase, P1on, 'o')
            plt.plot(phase, fitFunc(phase, *poff))
            plt.plot(phase, fitFunc(phase, *pon))
        plt.figure()
        plt.plot(g, deltaphase)
    return data


def sq_gate_QPT(Sample, measure=0, stats=600, name='sq_gate_QPT',
                 save=True, noisy=True, plot=True, Herald=False, gatename='X', delay=500 * ns, correct=False):
    sample, devs, qubits, Qubits = gc.loadQubits(Sample, measure, True)
    q0 = qubits[0]

    qptPrepOps = list(tomo.gen_qptPrep_tomo_ops(tomo.octomo_names, 1))
    i = np.arange(len(qptPrepOps))
    axes = [(i, "the initial state")]
    qstOps = list(tomo.gen_qst_tomo_ops(tomo.octomo_names, 1))
    kw = {gatename: 'gatename', 'stats': stats, 'delay': delay, 'Herald': Herald}
    deps = [('Probability', opLabel(ops) + ',' + stateLabel(state), '') for ops in qstOps for state
            in range(2)]
    dataset = sweeps.prepDataset(sample, name, axes, deps, measure=measure, kw=kw)

    def func(server, n):
        reqs = []
        for op in qstOps:
            alg = gc.Algorithm(agents=devs)
            q0 = alg.q0
            if Herald:
                alg[gates.Measure([q0], name="Herald", ringdown=False)]
                alg[gates.Wait([q0], delay)]
            alg[gates.Tomography([q0], qptPrepOps[n])]
            alg[Gatelist[gatename](q0)]
            alg[gates.Tomography([q0], op)]
            alg[gates.Measure([q0])]
            alg.compile()
            reqs.append(runQubits(server, alg.agents, stats, dataFormat='iqRaw'))
        reqs = yield FutureList(reqs)
        data = []
        for dat in reqs:
            probs = readout.iqToProbs(dat, alg.qubits, states=[0,1], herald=Herald)
            data.append(np.squeeze(probs))
        data = np.hstack(data)
        returnValue(data)

    data = sweeps.grid(func, axes, dataset=dataset, save=save, noisy=noisy)
    p = data[:, 1:]
    F = fMatrix(q0)
    if correct:
        p = correctVisibility(F, p, N=1)
    p = p.reshape((-1, 6, 2))
    rhosOut = np.array([tomo.qst_mle(d, 'octomo') for d in p])
    rhosIn = []
    for op in qstOps:
        initialstate = prestate(state=op[0])
        rhosIn.append(initialstate)
    chi = tomo.qpt(rhosIn, rhosOut, 'sigma')
    U = basis1[gatename]
    chi_th, inRhos, outRhos = tomo.gen_ideal_chi_matrix(U,'sigma',tomo.octomo_ops)
    if plot:
        tg.manhattan3d(chi.real, axesLabels=['I', 'X', 'Y', 'Z'])
        tg.manhattan3d(chi.imag, axesLabels=['I', 'X', 'Y', 'Z'])
        print 'the QPT fidelity is %.4f'%(fidelity(chi,chi_th))
    return chi


def testCz_QPT(Sample, measure=(0, 1), stats=1200, name='test Cz control', save=True, noisy=True, plot=False,
               correct=True, tBuf=20 * ns):
    sample, devs, qubits, Qubits = gc.loadQubits(Sample, measure, True)
    q0 = qubits[0]
    q1 = qubits[1]
    qptPrepOps = list(tomo.gen_qptPrep_tomo_ops(tomo.octomo_names, 2))
    i = np.arange(len(qptPrepOps))
    axes = [(i, "the initial state")]
    qstOps = list(tomo.gen_qst_tomo_ops(tomo.octomo_names, 2))
    kw = {'stats': stats}
    deps = [('Probability', opLabel(ops) + ',' + stateLabel(state), '') for ops in qstOps for state
            in range(4)]
    dataset = sweeps.prepDataset(sample, name, axes, deps, measure=measure, kw=kw)

    def func(server, n):
        reqs = []
        for op in qstOps:
            alg = gc.Algorithm(devs)
            q0 = alg.q0
            q1 = alg.q1
            alg[gates.Tomography([q0, q1], qptPrepOps[n])]
            alg[gates.Sync([q0, q1])]
            alg[gates.Wait([q0], tBuf)]
            alg[testCZ_gate([q0, q1], length=q0['testCzlen'], G=q0['Czstrength'], phase0=q0['testCzphase'],
                            phase1=q1['testCzphase'])]
            alg[gates.Wait([q0], tBuf)]
            alg[gates.Sync([q0, q1])]
            alg[gates.Tomography([q0, q1], op)]
            alg[gates.Measure([q0, q1])]
            alg.compile(correctXtalkZ=True, config=['q3', 'q4'])
            reqs.append(runQubits(server, alg.agents, stats, dataFormat='iqRaw'))
        reqs = yield FutureList(reqs)
        data = []
        for dat in reqs:
            probs = readout.iqToProbs(dat, alg.qubits, states=[0, 1], correlated=True)
            data.append(np.squeeze(probs))
        data = np.hstack(data)
        returnValue(data)

    data = sweeps.grid(func, axes, dataset=dataset, save=save, noisy=noisy)
    p = data[:, 1:]
    F = measfMatrix(q0, q1)
    if correct:
        p = correctVisibility(F, p, N=2)
    p = p.reshape((-1, 36, 4))
    rhosOut = np.array([tomo.qst_mle(d, 'octomo2') for d in p])
    rhosIn = []
    for name in qptPrepOps:
        print 'initial state is ' + name[0] + name[1]
        rhosIn.append(set_init_state(name))
    rhosIn = np.array(rhosIn)
    chi = tomo.qpt(rhosIn, rhosOut, 'sigma2')
    U = np.diag([1, 1, 1, -1])
    chi_th, inRhos, outRhos = tomo.gen_ideal_chi_matrix(U, 'sigma2', tomo.octomo_ops)
    if plot:
        tg.manhattan3d(chi.real)
        tg.manhattan3d(chi.imag)
        plt.title('the QPT fidelity is %.4f' % (
                    np.trace(np.dot(chi_th, chi)) / (np.trace(chi) * np.trace(chi_th))))
    return chi

# test CZ constructed by swap
def SwapCz_QPT(Sample, measure=(0, 1), stats=1200, name='test Cz control', save=True, noisy=True, plot=False,
               correct=True, tBuf=5 * ns):
    sample, devs, qubits, Qubits = gc.loadQubits(Sample, measure, True)
    q0 = qubits[0]
    q1 = qubits[1]
    qptPrepOps = list(tomo.gen_qptPrep_tomo_ops(tomo.octomo_names, 2))
    i = np.arange(len(qptPrepOps))
    axes = [(i, "the initial state")]
    qstOps = list(tomo.gen_qst_tomo_ops(tomo.octomo_names, 2))
    kw = {'stats': stats}
    deps = [('Probability', opLabel(ops) + ',' + stateLabel(state), '') for ops in qstOps for state
            in range(4)]
    dataset = sweeps.prepDataset(sample, name, axes, deps, measure=measure, kw=kw)

    def func(server, n):
        reqs = []
        for op in qstOps:
            alg = gc.Algorithm(devs)
            q0 = alg.q0
            q1 = alg.q1
            alg[gates.Tomography([q0, q1], qptPrepOps[n])]
            alg[gates.Sync([q0, q1])]
            alg[gates.Wait([q0], tBuf)]
            # alg[testCZ_gate([q0, q1], length=q0['testCzlen'], G=q0['Czstrength'], phase0=q0['testCzphase'],
            #                 phase1=q1['testCzphase'])]
            #alg[gates.Detune([q0], tlen=q0['Swapczlen']*2, amp=q0['Swapczamp'])]
            alg[SwapCZ([q0,q1], tlen=q0['Swapczlen']*2, amp=q0['Swapczamp'])]
            alg[gates.Wait([q0], tBuf)]
            alg[gates.Sync([q0, q1])]
            alg[gates.Tomography([q0, q1], op)]
            alg[gates.Measure([q0, q1])]
            alg.compile(correctXtalkZ=True, config=['q3', 'q4'])
            reqs.append(runQubits(server, alg.agents, stats, dataFormat='iqRaw'))
        reqs = yield FutureList(reqs)
        data = []
        for dat in reqs:
            probs = readout.iqToProbs(dat, alg.qubits, states=[0, 1], correlated=True)
            data.append(np.squeeze(probs))
        data = np.hstack(data)
        returnValue(data)

    data = sweeps.grid(func, axes, dataset=dataset, save=save, noisy=noisy)
    p = data[:, 1:]
    F = measfMatrix(q0, q1)
    if correct:
        p = correctVisibility(F, p, N=2)
    p = p.reshape((-1, 36, 4))
    rhosOut = np.array([tomo.qst_mle(d, 'octomo2') for d in p])
    rhosIn = []
    for name in qptPrepOps:
        print 'initial state is ' + name[0] + name[1]
        rhosIn.append(set_init_state(name))
    rhosIn = np.array(rhosIn)
    chi = tomo.qpt(rhosIn, rhosOut, 'sigma2')
    U = np.diag([1, 1, 1, -1])
    chi_th, inRhos, outRhos = tomo.gen_ideal_chi_matrix(U, 'sigma2', tomo.octomo_ops)
    if plot:
        tg.manhattan3d(chi.real)
        tg.manhattan3d(chi.imag)
        plt.title('the QPT fidelity is %.4f' % (
                    np.trace(np.dot(chi_th, chi)) / (np.trace(chi) * np.trace(chi_th))))
    return chi


def plot_QPT(dataset, Sample, measure=(0, 1), correct=True, plot=True, U=None):
    sample, devs, qubits, Qubits = gc.loadQubits(Sample, measure, True)
    q0 = qubits[0]
    q1 = qubits[1]
    qptPrepOps = list(tomo.gen_qptPrep_tomo_ops(tomo.octomo_names, 2))
    i = np.arange(len(qptPrepOps))

    p = dataset[:, 1:]
    F = measfMatrix(q0, q1)
    if correct:
        p = correctVisibility(F, p, N=2)
    p = p.reshape((-1, 36, 4))
    # temp = p[:,:,1]
    # p[:,:,1] = p[:,:,2]
    # p[:,:,2] = temp
    rhosOut = np.array([tomo.qst_mle(d, 'octomo2') for d in p])
    rhosIn = []
    for name in qptPrepOps:
        rhosIn.append(set_init_state(name))
    rhosIn = np.array(rhosIn)
    chi = tomo.qpt(rhosIn, rhosOut, 'sigma2')
    if U == None:
        U = np.diag([1, 1, 1, -1])
    chi_th, inRhos, outRhos = tomo.gen_ideal_chi_matrix(U, 'sigma2', tomo.octomo_ops)
    if plot:
        tg.manhattan3d(chi.real)
        tg.manhattan3d(chi.imag)
        tg.manhattan3d(chi_th.real)
        tg.manhattan3d(chi_th.imag)
        print 'the QPT fidelity is %.4f' % (
                    np.trace(np.dot(chi_th, chi)) / (np.trace(chi) * np.trace(chi_th)))
    return chi, rhosIn, rhosOut


def testCz_repeat(Sample, measure=(0, 1), repeat=20, init=('I', 'I'), stats=1200, name='test Cz control',
                  save=True, noisy=True, plot=False, correct=True, tBuf=20*ns):
    sample, devs, qubits, Qubits = gc.loadQubits(Sample, measure, True)
    q0 = qubits[0]
    q1 = qubits[1]
    i = np.arange(repeat)
    axes = [(i, "repeat number")]
    qstOps = list(tomo.gen_qst_tomo_ops(tomo.octomo_names, 2))
    kw = {'stats': stats}
    deps = [("|00> ", '', ''),
            ("|01>", '', ''),
            ("|10>", '', ''),
            ("|11>", '', '')]
    dataset = sweeps.prepDataset(sample, name, axes, deps, measure=measure, kw=kw)

    def func(server, n):
        reqs = []
        alg = gc.Algorithm(devs)
        q0 = alg.q0
        q1 = alg.q1
        alg[gates.Tomography([q0, q1], init)]
        alg[gates.Wait([q0], tBuf)]
        alg[testCZ_gate([q0, q1], length=q0['testCzlen'], G=q0['Czstrength'], phase0=q0['testCzphase'],
                        phase1=q1['testCzphase'])]
        alg[gates.Wait([q0], tBuf)]
        alg[gates.Measure([q0, q1])]
        alg.compile(correctXtalkZ=True, config=['q3', 'q4'])
        reqs.append(runQubits(server, alg.agents, stats, dataFormat='iqRaw'))
        reqs = yield FutureList(reqs)
        data = []
        for dat in reqs:
            probs = readout.iqToProbs(dat, alg.qubits, states=[0, 1], correlated=True)
            data.append(np.squeeze(probs))
        data = np.hstack(data)
        returnValue(data)

    data = sweeps.grid(func, axes, dataset=dataset, save=save, noisy=noisy)
    p = data[:, 1:]
    F = measfMatrix(q0, q1)
    print F
    if correct:
        p = correctVisibility(F, p, N=2)
    p = p.reshape((-1, repeat, 4))
    return p


def testCz_repeat_QST(Sample, measure=(0, 1), repeat=20, init=('I', 'I'), stats=1200, name='test Cz control',
                      save=True, noisy=True, plot=False, correct=True, tBuf=20*ns, CZ=True, tdelay=20*ns):
    sample, devs, qubits, Qubits = gc.loadQubits(Sample, measure, True)
    q0 = qubits[0]
    q1 = qubits[1]
    i = np.arange(1)
    axes = [(i, "repeat number")]
    qstOps = list(tomo.gen_qst_tomo_ops(tomo.octomo_names, 2))
    kw = {'stats': stats}
    deps = [('Probability', opLabel(ops) + ',' + stateLabel(state), '') for ops in qstOps for state
            in range(4)]
    dataset = sweeps.prepDataset(sample, name, axes, deps, measure=measure, kw=kw)

    def func(server, n):
        reqs = []
        for op in qstOps:
            alg = gc.Algorithm(devs)
            q0 = alg.q0
            q1 = alg.q1
            alg[gates.Tomography([q0, q1], init)]
            alg[gates.Sync([q0, q1])]
            alg[gates.Wait([q0], tBuf)]
            for j in range(repeat):
                if CZ:
                    alg[testCZ_gate([q0, q1], length=q0['testCzlen'], G=q0['Czstrength'], phase0=q0['testCzphase'],
                            phase1=q1['testCzphase'])]
                    alg[gates.Wait([q0], tdelay)]
                else:
                    alg[gates.Wait([q0], q0['testCzlen']*2)]

            alg[gates.Wait([q0], tBuf)]
            alg[gates.Sync([q0, q1])]
            alg[gates.Tomography([q0, q1], op)]
            alg[gates.Measure([q0, q1])]
            alg.compile(correctXtalkZ=True, config=['q3', 'q4'])
            reqs.append(runQubits(server, alg.agents, stats, dataFormat='iqRaw'))
        reqs = yield FutureList(reqs)
        data = []
        for dat in reqs:
            probs = readout.iqToProbs(dat, alg.qubits, states=[0, 1], correlated=True)
            data.append(np.squeeze(probs))
        data = np.hstack(data)
        returnValue(data)

    data = sweeps.grid(func, axes, dataset=dataset, save=save, noisy=noisy)
    p = data[:, 1:]
    F = measfMatrix(q0, q1)
    if correct:
        p = correctVisibility(F, p, N=2)
    p = p.reshape((-1, 36, 4))
    rhosOut = np.array([tomo.qst_mle(d, 'octomo2') for d in p])
    print rhosOut
    return rhosOut


def calSwapCoup(data, p):
    swapfreq, swaplen, p1 = dstools.arrange2Dscan(data)
    # experimental Prob data
    E = []
    for i in range(len(swapfreq)):
        deltaE = fitting.maxFreq(np.vstack((swaplen, p1[:, i])).T, 10000)
        E.append(deltaE['MHz'])
    E = np.array(E)
    plt.figure(figsize=(13, 4))
    plt.subplot(121)
    plt.pcolormesh(swapfreq, swaplen, p1, cmap='RdBu_r')
    plt.ylabel('Swap length (ns)', fontsize=15)
    plt.xlabel('Swap amplitude', fontsize=15)
    plt.xticks(fontsize=15)
    plt.yticks(fontsize=15)
    plt.colorbar()
    plt.subplot(122)
    plt.plot(swapfreq, E, 'ro')
    swapfunc = lambda x, A, x0, g: np.sqrt((A * (x - x0)) ** 2 + 4 * g ** 2)
    p, _ = curve_fit(swapfunc, swapfreq[0:50], E[0:50], p)
    swapfreq = np.linspace(-0.02, 0.02, 501) + p[1]
    plt.plot(swapfreq, swapfunc(swapfreq, *p), 'r')
    plt.ylabel('Swap frequency (MHz)', fontsize=15)
    plt.xlabel('Swap amplitude', fontsize=15)
    plt.xticks(fontsize=15)
    plt.yticks(fontsize=15)
    plt.tight_layout()
    print 'The resonace freq is %.3f GHz, the coupling stregth is %.3f MHz' % (p[1], p[2])
    print p
    return swapfreq, E


def testCNOT_QPT(Sample, measure=(0, 1), stats=1200, name='test CNOT', save=True, noisy=True, plot=False,
                 correct=True, tBuf=20*ns):
    sample, devs, qubits, Qubits = gc.loadQubits(Sample, measure, True)
    q0 = qubits[0]
    q1 = qubits[1]
    qptPrepOps = list(tomo.gen_qptPrep_tomo_ops(tomo.octomo_names, 2))
    i = np.arange(len(qptPrepOps))
    axes = [(i, "the initial state")]
    qstOps = list(tomo.gen_qst_tomo_ops(tomo.octomo_names, 2))
    kw = {'stats': stats}
    deps = [('Probability', opLabel(ops) + ',' + stateLabel(state), '') for ops in qstOps for state
            in range(4)]
    dataset = sweeps.prepDataset(sample, name, axes, deps, measure=measure, kw=kw)

    def func(server, n):
        reqs = []
        for op in qstOps:
            alg = gc.Algorithm(devs)
            q0 = alg.q0
            q1 = alg.q1
            alg[gates.Tomography([q0, q1], qptPrepOps[n])]
            alg[gates.PiHalfPulse([q0], phase=-np.pi / 2)]
            alg[gates.Sync([q0, q1])]
            alg[gates.Wait([q0], tBuf)]
            alg[testCZ_gate([q0, q1], length=q0['testCzlen'], G=q0['Czstrength'], phase0=q0['testCzphase'],
                            phase1=q1['testCzphase'])]
            alg[gates.Wait([q0], tBuf)]
            alg[gates.Sync([q0, q1])]
            alg[gates.PiHalfPulse([q0], phase=np.pi / 2)]
            alg[gates.Tomography([q0, q1], op)]
            alg[gates.Measure([q0, q1])]
            alg.compile(correctXtalkZ=True, config=['q3', 'q4'])
            reqs.append(runQubits(server, alg.agents, stats, dataFormat='iqRaw'))
        reqs = yield FutureList(reqs)
        data = []
        for dat in reqs:
            probs = readout.iqToProbs(dat, alg.qubits, states=[0, 1], correlated=True)
            data.append(np.squeeze(probs))
        data = np.hstack(data)
        returnValue(data)

    data = sweeps.grid(func, axes, dataset=dataset, save=save, noisy=noisy)
    p = data[:, 1:]
    F = measfMatrix(q0, q1)
    if correct:
        p = correctVisibility(F, p, N=2)
    p = p.reshape((-1, 36, 4))
    rhosOut = np.array([tomo.qst_mle(d, 'octomo2') for d in p])
    rhosIn = []
    for name in qptPrepOps:
        print 'initial state is ' + name[0] + name[1]
        rhosIn.append(set_init_state(name))
    rhosIn = np.array(rhosIn)
    chi = tomo.qpt(rhosIn, rhosOut, 'sigma2')
    U = np.diag([1,1,1,-1])
    U = dot3(np.kron(tomo.Ypi2,tomo.sigmaI),U,np.kron(tomo.Ympi2,tomo.sigmaI))
    chi_th, inRhos, outRhos = tomo.gen_ideal_chi_matrix(U, 'sigma2', tomo.octomo_ops)
    if plot:
        tg.manhattan3d(chi.real)
        tg.manhattan3d(chi.imag)
        plt.title('the QPT fidelity is %.4f' % (
                    np.trace(np.dot(chi_th, chi)) / (np.trace(chi) * np.trace(chi_th))))
    return chi


def testCNOT_repeat_QST(Sample, measure=(0, 1), repeat=20, init=('I', 'I'), stats=1200,
                        name='test Cz control', save=True, noisy=True, plot=False, correct=True, tBuf=20*ns):
    sample, devs, qubits, Qubits = gc.loadQubits(Sample, measure, True)
    q0 = qubits[0]
    q1 = qubits[1]
    i = np.arange(repeat)
    axes = [(i, "repeat number")]
    qstOps = list(tomo.gen_qst_tomo_ops(tomo.octomo_names, 2))
    kw = {'stats': stats,'name':init}
    deps = [('Probability', opLabel(ops) + ',' + stateLabel(state), '') for ops in qstOps for state
            in range(4)]
    dataset = sweeps.prepDataset(sample, name, axes, deps, measure=measure, kw=kw)

    def func(server, n):
        reqs = []
        for op in qstOps:
            alg = gc.Algorithm(devs)
            q0 = alg.q0
            q1 = alg.q1
            alg[gates.Tomography([q0, q1], init)]
            alg[gates.PiHalfPulse([q0], phase=-np.pi / 2)]
            alg[gates.Sync([q0, q1])]
            alg[gates.Wait([q0], tBuf)]
            alg[testCZ_gate([q0, q1], length=q0['testCzlen'], G=q0['Czstrength'], phase0=q0['testCzphase'],
                            phase1=q1['testCzphase'])]
            alg[gates.Wait([q0], tBuf)]
            alg[gates.Sync([q0, q1])]
            alg[gates.PiHalfPulse([q0], phase=np.pi / 2)]
            alg[gates.Tomography([q0, q1], op)]
            alg[gates.Measure([q0, q1])]
            alg.compile(correctXtalkZ=True, config=['q3', 'q4'])
            reqs.append(runQubits(server, alg.agents, stats, dataFormat='iqRaw'))
        reqs = yield FutureList(reqs)
        data = []
        for dat in reqs:
            probs = readout.iqToProbs(dat, alg.qubits, states=[0, 1], correlated=True)
            data.append(np.squeeze(probs))
        data = np.hstack(data)
        returnValue(data)

    data = sweeps.grid(func, axes, dataset=dataset, save=save, noisy=noisy)
    p = data[:, 1:]
    F = measfMatrix(q0, q1)
    if correct:
        p = correctVisibility(F, p, N=2)
    p = p.reshape((-1, 36, 4))
    rhosOut = np.array([tomo.qst_mle(d, 'octomo2') for d in p])
    print rhosOut
    return rhosOut


#~~~~~~~~~~~~~~~~~~~RB

from pyle.dataking.benchmarking import randomizedBechmarking as rb

class testSwapCZ_RBCliffordMultiQubit(Gate):
    def __init__(self, agents, gateList, sync=True):
        """
        build gate from gateList for multiQubit.
        @param agents: qubits
        @param gateList: a list of gate string, should be the same format of rbClass.randGen
        @param alphaList: a list of alpha, [alpha for q0, alpha for q1, ... ]
        @param sync: sync for each clifford gate, default is True
        """
        self.gateList = gateList
        self.sync = sync
        self.gateOps = {
            'I': lambda q: gates.Wait([q], q.get('identityLen', np.min([q['piLen']['ns'], q['piHalfLen']['ns']]) * ns)),
            'IW': lambda q: gates.Wait([q], q['identityWaitLen']),
            'IWSE': lambda q: gates.EchoWait([q], q['identityWaitLen']),
            'SE': lambda q: gates.Echo([q], q['identityWaitLen']),
            'IGN': lambda q: gates.Wait([q], 0 * ns),
            'X': lambda q: gates.PiPulse([q]),
            'Y': lambda q: gates.PiPulse([q], phase=np.pi/2.),
            'X/2': lambda q: gates.PiHalfPulse([q]),
            'Y/2': lambda q: gates.PiHalfPulse([q], phase=np.pi/2.),
            '-X': lambda q: gates.PiPulse([q], phase=np.pi),
            '-Y': lambda q: gates.PiPulse([q], phase=3 * np.pi/2.),
            '-X/2': lambda q: gates.PiHalfPulse([q], phase=np.pi),
            '-Y/2': lambda q: gates.PiHalfPulse([q], phase=3*np.pi/2.),
            'H': lambda q: gates.FastRFHadamard([q]),
            'Z': lambda q: gates.Detune([q]),
            'Zpi': lambda q: gates.PiPulseZ([q]),
            'Zpi/2': lambda q: gates.PiHalfPulseZ([q]),
            #"CZ": lambda q1, q2: testCZ_gate([q1, q2]),
            "CZ": lambda q1, q2: SwapCZ([q1, q2]),
            "CNOT": lambda q1, q2: gates.CNOT([q1, q2])
        }
        super(testSwapCZ_RBCliffordMultiQubit, self).__init__(agents)

    def updateAgents(self):
        pass

    def setSubgates(self, agents):
        subgates = []
        twoQubitGates = ["CZ", "CNOT"]
        for cliffordGate in self.gateList:
            ops = rb.gate2OpList(len(agents), cliffordGate)
            for op in ops:
                twoQubitGatePresent = any([twoQubitGateElem in op for twoQubitGateElem in twoQubitGates])
                if twoQubitGatePresent:
                    subgates.extend([gates.Sync(agents),
                                     self.gateOps[op[0]](agents[0],agents[1]),
                                     gates.Sync(agents)])
                else:
                    for sq_op, ag in zip(op, agents):
                        subgates.append(self.gateOps[sq_op](ag))
            if self.sync:
                subgates.append(gates.Sync(agents))
        self.subgates = subgates


def randomizedBenchmarking(Sample, measure=0, ms=None, k=30, interleaved=False, maxtime=14*us,
                           name='SQ RB Clifford', stats=900, plot=True, save=True, noisy=False):
    """
    single Qubit RB Clifford,
    ms is a sequence for the number of gates,
    k is the repetition of each number of gates
    interleaved is the gate name, in the format ["X"], or ["X", "Y/2"]
    available gate names are
        ["I", "X", "Y", "X/2", "Y/2", "-X/2", "-Y/2", "-X", "-Y"]

    """

    sample, devs, qubits = gc.loadQubits(Sample, measure)
    rbClass = rb.RBClifford(1, False)

    if ms is None:
        rbClass.setGateLength([devs[measure]])
        m_max = rb.getlength(rbClass, maxtime, interleaved=interleaved, )
        ms = np.unique([int(m) for m in np.logspace(0, np.log10(m_max), 30, endpoint=True)])

    def getSequence(m):
        sequence = rbClass.randGen(m, interleaved=interleaved, finish=True)
        return sequence

    axesname = 'm - number of Cliffords'
    if interleaved:
        name += ' interleaved: ' + str(interleaved)
        axesname = "m - number of set of Clifford+interleaved"

    axes = [(ms, axesname), (range(k), 'sequence')]
    deps = [("Sequence Fidelity", "", "")]

    kw = {"stats": stats, "interleaved": interleaved, 'k': k, 'axismode': 'm', "maxtime": maxtime}

    dataset = sweeps.prepDataset(sample, name, axes, deps, measure=measure, kw=kw)

    def func(server, currM, currK):
        print("m = {m}, k = {k}".format(m=currM, k=currK))
        gate_list = getSequence(currM)
        alg = gc.Algorithm(devs)
        q0 = alg.q0
        alg[gates.RBCliffordSingleQubit([q0], gate_list)]
        alg[gates.Measure([q0])]
        alg.compile()
        data = yield runQubits(server, alg.agents, stats, dataFormat='iqRaw')
        probs = readout.iqToProbs(data, alg.qubits)
        returnValue([np.squeeze(probs)[0]])

    data = sweeps.grid(func, axes, dataset=dataset, save=save, noisy=noisy)

    if plot:
        sq.plotRBClifford(data)

    return data

def testSwapCZ_randomizedBenchmarking(Sample, measure=(0,1), ms=None, k=30, interleaved=False, maxtime=14*us,
                           name='testSwapCZ RB Clifford', stats=900, plot=True, save=True, noisy=False):
    """
    single Qubit RB Clifford,
    ms is a sequence for the number of gates,
    k is the repetition of each number of gates
    interleaved is the gate name, in the format ["X"], or ["X", "Y/2"]
    available gate names are
        ["I", "X", "Y", "X/2", "Y/2", "-X/2", "-Y/2", "-X", "-Y"]

    """

    sample, devs, qubits = gc.loadQubits(Sample, measure)
    rbClass = rb.RBClifford(2, False)

    def getSequence(m):
        sequence = rbClass.randGen(m, interleaved=interleaved, finish=True)
        return sequence

    axesname = 'm - number of Cliffords'
    if interleaved:
        name += ' interleaved: ' + str(interleaved)
        axesname = "m - number of set of Clifford+interleaved"

    axes = [(ms, axesname), (range(k), 'sequence')]
    deps = [("Sequence Fidelity", "", "")]

    kw = {"stats": stats, "interleaved": interleaved, 'k': k, 'axismode': 'm', "maxtime": maxtime}

    dataset = sweeps.prepDataset(sample, name, axes, deps, measure=measure, kw=kw)

    def func(server, currM, currK):
        print("m = {m}, k = {k}".format(m=currM, k=currK))
        gate_list = getSequence(currM)
        print gate_list
        alg = gc.Algorithm(devs)
        q0 = alg.q0
        q1 = alg.q1
        alg[testSwapCZ_RBCliffordMultiQubit([q0,q1], gate_list)]
        alg[gates.Measure([q0, q1])]
        alg.compile(correctXtalkZ=True, config=['q3', 'q4'])
        data = yield runQubits(server, alg.agents, stats, dataFormat='iqRaw')
        probs = np.squeeze(
            readout.iqToProbs(data, alg.qubits, states=[0, 1], correlated=True)).flat
        returnValue([probs[0]])

    data = sweeps.grid(func, axes, dataset=dataset, save=save, noisy=noisy)
    if plot:
        sq.plotRBClifford(data)

    return data


def test_Cz_Zdelay(Sample, delay, measure=(0, 1), control=False, stats=1200, tBuf=5 * ns,
                name='test Cz control', save=True, noisy=True, repeatNum=2, CZdistance=100*ns):
    sample, devs, qubits = gc.loadQubits(Sample, measure=measure)

    axes = [(delay, 'delay [ns]')]

    deps = [("Q3|0>", '', ''), ("Q3|1>", '', ''),
            ("Q4|0>", '', ''), ("Q4|1>", '', '')]
    kw = {"stats": stats, 'tBuf': tBuf, 'Control': control}

    dataset = sweeps.prepDataset(sample, name, axes, deps, measure=measure, kw=kw)

    def func(server, currdelay):
        alg = gc.Algorithm(devs)
        q0 = alg.q0
        q1 = alg.q1
        if control:
            alg[gates.PiPulse([q1])]
        for i in range(repeatNum):
            alg[testCZ_gate([q0, q1], length=q0['testCzlen'], G=q0['Czstrength'])]
            if (i==0) and (repeatNum>1) :
                alg[gates.Wait([q0], currdelay)]
                alg[gates.PiHalfPulse([q0])]
                alg[gates.Wait([q0], tBuf)]
                alg[gates.PiHalfPulse([q0],phase=np.pi/2)]
            alg[gates.Wait([q0], CZdistance)]
        if repeatNum==1:
            alg[gates.Wait([q0], currdelay)]
            alg[gates.PiHalfPulse([q0])]
            alg[gates.Wait([q0], tBuf)]
            alg[gates.PiHalfPulse([q0],phase=np.pi/2)]
        if control:
            alg[gates.Sync([q0, q1])]
            alg[gates.PiPulse([q1])]
        alg[gates.Measure([q0, q1])]
        alg.compile(correctXtalkZ=True, config=['q3', 'q4'])
        data = yield runQubits(server, alg.agents, stats, dataFormat='iqRaw')
        probs = np.squeeze(
            readout.iqToProbs(data, alg.qubits, states=[0, 1], correlated=False)).flat
        returnValue([probs[0], probs[1], probs[2], probs[3]])

    data = sweeps.grid(func, axes, dataset=dataset, save=save, noisy=noisy)
    return data


def zPulseTailDecay(Sample, measure=(0,1), waitTime=500*ns, delayPoints=51, height=-1.0, zpulseLen=0.5*us,
                    tOffset=0*ns, measDelay=0*ns, doRelative=True, stats=1200, name='Z Tail Decay',
                    reset=True, fixYTime=False, save=True,repeatNum=1, correctXtalkZ=True, control=True):
    """
    waitTime should be 3x or longer than suspected ringdown time.
    after a step, wait delay, then X/2 and wait for a time, finally Y/2.
    1st order sensitity according RarendsNat14

    only update settlingRates
    """

    delay = np.linspace(0, waitTime['ns'], delayPoints)*ns

    sample, devs, qubits, Qubits = gc.loadQubits(Sample, measure=measure, write_access=True)

    axes = [(delay, 'Delay')]
    if doRelative:
        deps = [('Probablity', "no op", ""), ("Probability", "z pulse", ""),
                ("Relative Probability", "", "")]
    else:
        deps = [("Probablity", "z pulse", "")]

    kw = {'stats': stats, 'z pulse height': height, 'z pulse length': zpulseLen, 'waitTime': waitTime,
          'doRelative': doRelative, 'reset': reset, 'tOffset': tOffset, 'measDelay': measDelay,
          'fixYTime': fixYTime,'repeatNum':repeatNum, 'correctXtalkZ':correctXtalkZ}
    dataset = sweeps.prepDataset(sample, name, axes, deps, measure=measure, kw=kw)

    if reset:
        devs[measure[0]]['settlingRates'] = []
        devs[measure[0]]['settlingAmplitudes'] = []
    def func(server, currDelay):
        currDelay -= qubits[measure[0]]['piLen']/2.0

        alg = gc.Algorithm(devs)
        q0 = alg.qubits[0]
        q1 = alg.qubits[1]
        if control:
            alg[gates.PiPulse([q1])]
        for i in range(repeatNum):
            alg[gates.Wait([q0], 30*ns)]
            alg[gates.Detune([q0], zpulseLen, amp=height)]
            # alg[testCZ_gate([q1, q0], length=q1['testCzlen'], G=q1['Czstrength'], phase0=q1['testCzphase'],
            #                 phase1=q0['testCzphase'])]
            alg[gates.Sync([q0, q1])]
        alg[gates.Wait([q0], currDelay)]
        alg[gates.PiHalfPulse([q0])] # X/2
        if fixYTime:
            alg[gates.Wait([q0], waitTime-currDelay)]
        else:
            alg[gates.Wait([q0], tOffset)]
        alg[gates.PiHalfPulse([q0], phase=np.pi/2)] # Y/2
        alg[gates.Wait([q0], measDelay)]
        alg[gates.Measure([q0,q1])]
        alg.compile(correctXtalkZ=correctXtalkZ, config=['q3', 'q4'])
        dataZ = yield runQubits(server, alg.agents, stats, dataFormat='iqRaw')
        probZ = np.squeeze(readout.iqToProbs(dataZ, alg.qubits, states=[0, 1], correlated=False)).flatten()
        probZ = probZ[measure[0]*2+1]

        if doRelative:
            # no z pulse
            alg = gc.Algorithm(devs)
            q0 = alg.qubits[0]
            q1 = alg.qubits[1]
            alg[gates.Detune([q1], zpulseLen, amp=0)]
            alg[gates.Sync([q0, q1])]
            alg[gates.Wait([q0], currDelay)]
            alg[gates.PiHalfPulse([q0])]  # X/2
            if fixYTime:
                alg[gates.Wait([q0], waitTime - currDelay)]
            else:
                alg[gates.Wait([q0], tOffset)]
            alg[gates.PiHalfPulse([q0], phase=np.pi/2)]  # Y/2
            alg[gates.Wait([q0], measDelay)]
            alg[gates.Measure([q0,q1])]
            alg.compile(correctXtalkZ=correctXtalkZ, config=['q3', 'q4'])
            dataNoop = yield runQubits(server, alg.agents, stats, dataFormat='iqRaw')
            probNoop = np.squeeze(readout.iqToProbs(dataNoop, alg.qubits, states=[0, 1], correlated=False)).flatten()
            probNoop = probNoop[measure[0]*2+1]
            dat = np.hstack([probNoop, probZ, (probZ-probNoop)])
            returnValue(dat)
        else:
            returnValue([probZ[1]])

    data = sweeps.grid(func, axes, dataset=dataset, save=save, noisy=True)

    return data



def zPulseTailDecay2(Sample, measure=(0,1), waitTime=500*ns, delayPoints=51, height=-1.0, zpulseLen=0.5*us,
                    tOffset=5*ns, measDelay=0*ns, doRelative=True, stats=1200, name='Z Tail Decay',
                    reset=True, fixYTime=False, save=True,repeatNum=1, correctXtalkZ=True, control=True):
    """
    waitTime should be 3x or longer than suspected ringdown time.
    after a step, wait delay, then X/2 and wait for a time, finally Y/2.
    1st order sensitity according RarendsNat14

    only update settlingRates
    """

    delay = np.linspace(0, waitTime['ns'], delayPoints)*ns

    sample, devs, qubits, Qubits = gc.loadQubits(Sample, measure=measure, write_access=True)

    axes = [(delay, 'Delay')]
    if doRelative:
        deps = [('Probablity', "no op", ""), ("Probability", "z pulse", ""),
                ("Relative Probability", "", "")]
    else:
        deps = [("Probablity", "z pulse", "")]

    kw = {'stats': stats, 'z pulse height': height, 'z pulse length': zpulseLen, 'waitTime': waitTime,
          'doRelative': doRelative, 'reset': reset, 'tOffset': tOffset, 'measDelay': measDelay,
          'fixYTime': fixYTime,'repeatNum':repeatNum, 'correctXtalkZ':correctXtalkZ}
    dataset = sweeps.prepDataset(sample, name, axes, deps, measure=measure, kw=kw)

    if reset:
        devs[measure[0]]['settlingRates'] = []
        devs[measure[0]]['settlingAmplitudes'] = []
    def func(server, currDelay):
        alg = gc.Algorithm(devs)
        q0 = alg.qubits[0]
        q1 = alg.qubits[1]
        if control:
            alg[gates.PiPulse([q1])]
        # alg[gates.Detune([q0], zpulseLen, amp=height)]
        alg[gates.PiHalfPulse([q0])] # X/2
        alg[gates.Detune([q0], zpulseLen, amp=height)]
        # alg[testCZ_gate([q0, q1], length=q0['testCzlen'], G=q0['Czstrength'], phase0=q0['testCzphase'],
        #                  phase1=q0['testCzphase'])]
        alg[gates.Sync([q0, q1])]
        alg[gates.Wait([q0], tOffset)]

        alg[gates.Wait([q0], currDelay)]

        alg[gates.Wait([q0], tOffset)]
        alg[gates.Detune([q0], zpulseLen, amp=height)]
        # alg[testCZ_gate([q0, q1], length=q0['testCzlen'], G=q0['Czstrength'], phase0=q0['testCzphase'],
        #                  phase1=q0['testCzphase'])]
        alg[gates.PiHalfPulse([q0], phase=np.pi/2)] # Y/2
        alg[gates.Wait([q0], measDelay)]
        alg[gates.Measure([q0,q1])]
        alg.compile(correctXtalkZ=correctXtalkZ, config=['q3', 'q4'])
        dataZ = yield runQubits(server, alg.agents, stats, dataFormat='iqRaw')
        probZ = np.squeeze(readout.iqToProbs(dataZ, alg.qubits, states=[0, 1], correlated=False)).flatten()
        probZ = probZ[measure[0]*2+1]

        if doRelative:
            # no z pulse
            alg = gc.Algorithm(devs)
            q0 = alg.qubits[0]
            q1 = alg.qubits[1]
            alg[gates.Detune([q1], zpulseLen, amp=0)]
            alg[gates.Sync([q0, q1])]
            alg[gates.Wait([q0], currDelay)]
            alg[gates.PiHalfPulse([q0])]  # X/2
            if fixYTime:
                alg[gates.Wait([q0], waitTime - currDelay)]
            else:
                alg[gates.Wait([q0], tOffset)]
            alg[gates.PiHalfPulse([q0], phase=np.pi/2)]  # Y/2
            alg[gates.Wait([q0], measDelay)]
            alg[gates.Measure([q0,q1])]
            alg.compile(correctXtalkZ=correctXtalkZ, config=['q3', 'q4'])
            dataNoop = yield runQubits(server, alg.agents, stats, dataFormat='iqRaw')
            probNoop = np.squeeze(readout.iqToProbs(dataNoop, alg.qubits, states=[0, 1], correlated=False)).flatten()
            probNoop = probNoop[measure[0]*2+1]
            dat = np.hstack([probNoop, probZ, (probZ-probNoop)])
            returnValue(dat)
        else:
            returnValue([probZ[1]])

    data = sweeps.grid(func, axes, dataset=dataset, save=save, noisy=True)

    return data



def zPulseTailAmp(Sample, measure=0, ampScale=st.r[-0.5:0.5:0.01], waitTime=500*ns, delayPoints=51,
                  height=-1.0, zpulseLen=2*us, tOffset=0*ns, measDelay=0*ns, doRelative=True, stats=1200,
                  name='Z Tailing Amp',  save=True, repeatNum=20):
    delay = np.linspace(0, waitTime['ns'], delayPoints)*ns
    sample, devs, qubits = gc.loadQubits(Sample, measure=measure)

    if len(qubits[measure]['settlingRates']) > 1:
        ampArr = qubits[measure]['settlingAmplitudes']
    else:
        ampArr = np.array([1.0])
    print("Original settlingAmplitudes: %s" %ampArr)

    axes = [(ampScale, 'amp scale'), (delay, 'Delay')]
    if doRelative:
        deps = [('Probability', 'no op', ''), ("Probability", "z pulse", ""),
                ("Relative Probability", '', "")]
    else:
        deps = [("Probability", "z pulse", "")]

    kw = {'stats': stats, 'z pulse height': height, 'z pulse length': zpulseLen, "waitTime": waitTime,
          "delayPoints": delay, "tOffset": tOffset, "measDelay": measDelay, "doRelative": doRelative,
           'repeatNum':repeatNum}

    dataset = sweeps.prepDataset(sample, name, axes, deps, measure=measure, kw=kw)

    def func(server, currAmp, currDelay):
        alg = gc.Algorithm(devs)
        q0 = alg.qubits[0]
        q0['settlingAmplitudes'] = currAmp * ampArr
        alg[gates.PiHalfPulse([q0])]
        for i in range(repeatNum):
            alg[gates.Detune([q0], tlen=zpulseLen, amp=height)]
            if (repeatNum>1)&(i<repeatNum-1):
                alg[gates.Wait([q0], currDelay)]
        if repeatNum==1:
            alg[gates.Wait([q0], currDelay)]
        alg[gates.Wait([q0], tOffset)]
        alg[gates.PiHalfPulse([q0], phase=np.pi/2)]
        alg[gates.Wait([q0], measDelay)]
        alg[gates.Measure([q0])]
        alg.compile()
        dataZ = yield runQubits(server, alg.agents, stats)
        probZ = np.squeeze(readout.iqToProbs(dataZ, alg.qubits))

        if doRelative:
            alg = gc.Algorithm(devs)
            q0 = alg.qubits[0]
            q0['settlingAmplitudes'] = currAmp * ampArr
            alg[gates.PiHalfPulse([q0])]
            for i in range(repeatNum):
                alg[gates.Detune([q0], tlen=zpulseLen, amp=0.0)]
                if (repeatNum>1)&(i<repeatNum-1):
                    alg[gates.Wait([q0], currDelay)]
            if repeatNum==1:
                alg[gates.Wait([q0], currDelay)]
            alg[gates.Wait([q0], tOffset)]
            alg[gates.PiHalfPulse([q0], phase=np.pi / 2)]
            alg[gates.Wait([q0], measDelay)]
            alg[gates.Measure([q0])]
            alg.compile()
            dataNoop = yield runQubits(server, alg.agents, stats)
            probNoop = np.squeeze(readout.iqToProbs(dataNoop, alg.qubits))
            returnValue([probNoop[1], probZ[1], probZ[1]-probNoop[1]])
        else:
            returnValue([probZ[1]])

    data = sweeps.grid(func, axes, dataset=dataset, save=save, noisy=True)

    return data


def zPulseTailDecayQST2(Sample, measure, delay=None, height=-1.0, zpulseLen=2*us, reset=True,
                       name='Z Tail Decay QST', stats=900, plot=True, update=False,
                       save=True,  doRelative=True, repeatNum=2, measuredelay=2*ns):

    """
    Get settling rate
    reset: run without correction
    update: store new values to registry
    for Z pulse correction
    sq.detuneTailTimeQST(s,0,reset=False,update=False,delay = np.logspace(np.log10(1), np.log10(500), 250)*ns,fixPiTime=True)
    """
    # generate tomo operations
    ops = tomo.gen_qst_tomo_ops(tomo.octomo_names, 1 )
    opList = [op for op in ops]

    sample, devs, qubits, Qubits = gc.loadQubits(Sample, measure, write_access=True)

    if delay is None:
        delay = [np.arange(1, 20, 1), np.arange(20, 60, 2),
                 np.arange(60, 200, 10), np.arange(200, 501, 20)]
        delay = np.hstack(delay) * ns

    axes = [(delay, 'Ramsey Delay')]
    deps = [('Ramsey Relative Phase', '', '')]
    kw = {'stats': stats, 'doRelative':doRelative, 'z pulse height': height,
          'z pulse length': zpulseLen, 'reset': reset, 'repeatNum':repeatNum,'measuredelay':measuredelay}

    dataset = sweeps.prepDataset(sample, name, axes, deps, measure=measure, kw=kw)

    origSettlingRates = qubits[measure]['settlingRates']
    origSettlingAmplitudes = qubits[measure]['settlingAmplitudes']

    def func(server, currDelay):
        reqs = []
        if doRelative:
            for op in opList:
                alg = gc.Algorithm(agents=devs)
                q0 = alg.qubits[0]
                if reset:
                    q0['settlingRates'] = []
                    q0['settlingAmplitudes'] = []
                alg[gates.PiHalfPulse([q0])]
                for i in range(repeatNum):
                    alg[gates.Detune([q0], tlen=zpulseLen, amp=0.0)]
                    if (repeatNum>1)&(i<repeatNum-1):
                        alg[gates.Wait([q0], currDelay)]
                if repeatNum==1:
                    alg[gates.Wait([q0], currDelay)]
                alg[gates.Wait([q0], measuredelay)]
                alg[gates.Tomography([q0], op)]
                alg[gates.Measure([q0])]
                alg.compile()
                reqs.append(runQubits(server, alg.agents, stats, dataFormat='iqRaw'))

        for op in opList:
            alg = gc.Algorithm(agents=devs)
            q0 = alg.qubits[0]
            if reset:
                q0['settlingRates'] = []
                q0['settlingAmplitudes'] = []
            alg[gates.PiHalfPulse([q0])]
            for i in range(repeatNum):
                alg[gates.Detune([q0], tlen=zpulseLen, amp=height)]
                if (repeatNum>1)&(i<repeatNum-1):
                    alg[gates.Wait([q0], currDelay)]
            if repeatNum==1:
                    alg[gates.Wait([q0], currDelay)]
            alg[gates.Wait([q0], measuredelay)]
            alg[gates.Tomography([q0], op)]
            alg[gates.Measure([q0])]
            alg.compile()
            reqs.append(runQubits(server, alg.agents, stats, dataFormat='iqRaw'))
        dataControl    = []
        dataExperiment = []

        if doRelative:
            results = yield FutureList(reqs)
            for r in results[:len(opList)]:
                prob = np.squeeze(readout.iqToProbs(r, alg.qubits))
                dataControl.append(prob)
            for r in results[len(opList):]:
                prob = np.squeeze(readout.iqToProbs(r, alg.qubits))
                dataExperiment.append(prob)

            rho = tomo.qst(np.array(dataControl), 'octomo')
            phaseControl = np.angle(rho[0, 1])

            rho = tomo.qst(np.array(dataExperiment), 'octomo')
            phaseExperiment = np.angle(rho[0, 1])-q0['DetunePhase']*repeatNum

            deltaPhi = -(phaseControl-phaseExperiment)
        else:
            results = yield FutureList(reqs)
            for r in results:
                prob = np.squeeze(readout.iqToProbs(r, alg.qubits))
                dataControl.append(prob)

            rho = tomo.qst(np.array(dataControl), 'octomo')
            phaseControl = np.angle(rho[0, 1])

            deltaPhi = phaseControl-q0['DetunePhase']*repeatNum


        def mapToBranch(phi):
            while phi<=-np.pi/4. or phi>7/4.*np.pi:
                if phi<=-np.pi/4.:
                    phi+=2*np.pi
                elif phi>7/4.*np.pi:
                    phi-=2*np.pi
            return phi

        returnValue([mapToBranch(deltaPhi)])

    data = sweeps.grid(func, axes, dataset=dataset, save=save, noisy=True)

    t = data[:,0]
    phase = data[:,1]
    idx = t > 0
    t = t[idx]
    phase = phase[idx]
    phase = np.unwrap(phase)
    phaseToFit = phase.copy()
    v, cov, fitFunc = fitting.fitCurve('exponential', t, phaseToFit,
                                       [phaseToFit[0] - phaseToFit[-1], 50, phaseToFit[-1]])
    decayTime = v[1]
    print v
    print decayTime
    if plot:
        plt.figure()
        plt.plot(t, phase, 'o', label='data')
        plt.plot(t, fitFunc(t, *v), '-', label='fit')
        plt.xlabel('Ramsey Delay')
        plt.ylabel('Ramsey Phase')
    if update:
        Q = Qubits[measure]
        if reset:
            Q['settlingRates'] = [1. / decayTime]
            Q['settlingAmplitudes'] = [0.0]
        else:
            Q['settlingRates'] = list(origSettlingRates )+ [1. / decayTime]
            Q['settlingAmplitudes'] = list(origSettlingAmplitudes) + [0.0]

    return data




def zPulseTailAmpQST2(Sample, measure, ampScale=st.r[-0.1:0.1:0.01], delay=None, height=-1.0,
                     zpulseLen=2*us,  name='Z Tail Amp QST', stats=900, save=True, repeatNum=2,measuredelay=2*ns):
    # generate tomo operations
    ops = tomo.gen_qst_tomo_ops(tomo.octomo_names, 1 )
    opList = [op for op in ops]

    sample, devs, qubits, Qubits = gc.loadQubits(Sample, measure, write_access=True)

    if delay is None:
        delay = [np.arange(1, 20, 1), np.arange(20, 60, 2),
                 np.arange(60, 200, 10), np.arange(200, 501, 20)]
        delay = np.hstack(delay) * ns

    axes = [(ampScale, 'scale'), (delay, 'Ramsey Delay')]
    deps = [('Ramsey Relative Phase', '', '')]
    kw = {'stats': stats, 'z pulse height': height,
          'z pulse length': zpulseLen, 'repeatNum':repeatNum,'measuredelay':measuredelay}
    if len(qubits[measure]['settlingRates']) > 1:
        ampArr = qubits[measure]['settlingAmplitudes']
    else:
        ampArr = np.array([1.0])
    print("Original settlingAmplitudes: %s" %ampArr)

    dataset = sweeps.prepDataset(sample, name, axes, deps, measure=measure, kw=kw)

    def func(server, currAmp, currDelay):

        reqs = []
        for op in opList:
            alg = gc.Algorithm(agents=devs)
            q0 = alg.qubits[0]
            q0['settlingAmplitudes'] = ampArr*currAmp
            alg[gates.PiHalfPulse([q0])]
            for i in range(repeatNum):
                alg[gates.Detune([q0], tlen=zpulseLen, amp=0.0)]
                if (repeatNum>1)&(i<repeatNum-1):
                    alg[gates.Wait([q0], currDelay)]
            alg[gates.Wait([q0], measuredelay)]
            alg[gates.Tomography([q0], op)]
            alg[gates.Measure([q0])]
            alg.compile()
            reqs.append(runQubits(server, alg.agents, stats, dataFormat='iqRaw'))

        for op in opList:
            alg = gc.Algorithm(agents=devs)
            q0 = alg.qubits[0]
            q0['settlingAmplitudes'] = ampArr*currAmp
            alg[gates.PiHalfPulse([q0])]
            for i in range(repeatNum):
                alg[gates.Detune([q0], tlen=zpulseLen, amp=height)]
                if (repeatNum>1)&(i<repeatNum-1):
                    alg[gates.Wait([q0], currDelay)]
            alg[gates.Wait([q0], measuredelay)]
            alg[gates.Tomography([q0], op)]
            alg[gates.Measure([q0])]
            alg.compile()
            reqs.append(runQubits(server, alg.agents, stats, dataFormat='iqRaw'))
        dataControl    = []
        dataExperiment = []

        results = yield FutureList(reqs)
        for r in results[:len(opList)]:
            prob = np.squeeze(readout.iqToProbs(r, alg.qubits))
            dataControl.append(prob)
        for r in results[len(opList):]:
            prob = np.squeeze(readout.iqToProbs(r, alg.qubits))
            dataExperiment.append(prob)

        rho = tomo.qst(np.array(dataControl), 'octomo')
        phaseControl = np.angle(rho[0, 1])

        rho = tomo.qst(np.array(dataExperiment), 'octomo')
        phaseExperiment = np.angle(rho[0, 1])-repeatNum*q0['DetunePhase']

        deltaPhi = -(phaseControl-phaseExperiment)

        def mapToBranch(phi):
            while phi<=-np.pi/4. or phi>7/4.*np.pi:
                if phi<=-np.pi/4.:
                    phi+=2*np.pi
                elif phi>7/4.*np.pi:
                    phi-=2*np.pi
            return phi

        returnValue([mapToBranch(deltaPhi)])

    data = sweeps.grid(func, axes, dataset=dataset, save=save, noisy=True)

    return data


def population_1(Sample, measure=(0, 1), reps=100, state=1, herald=False, stats=3000,
                    name='readout fidelity', save=True, update=True, noisy=True, control=False, prob_correlated=False):
    sample, devs, qubits, Qubits = gc.loadQubits(Sample, measure, write_access=True)

    axes = [(range(reps), "repetition")]
    deps = readout.genProbDeps(qubits, measure, states=range(state+1))
    kw = {"states": range(state+1), 'stats': stats, 'herald': herald}

    name += " " + " ".join(["|%d>" %l for l in range(state+1)])

    dataset = sweeps.prepDataset(sample, name, axes, deps, measure=measure, kw=kw)

    def func(server, curr):
        reqs = []
        for currState in range(state+1):
            alg = gc.Algorithm(devs)
            q0 = alg.q0
            q1 = alg.q1
            if herald:
                alg[gates.Measure([q0], name='herald')]
                alg[gates.Wait([q0], q0['readoutRingdownLen'])]
            if control :
                alg[gates.Sync([q0, q1])]
                alg[gates.PiPulse([q1])]
                alg[gates.Sync([q0, q1])]
            alg[gates.MoveToState([q0], 0, currState)]
            alg[gates.Measure([q0, q1])]
            alg.compile()
            data = yield runQubits(server, alg.agents, stats, dataFormat='iqRaw')
            probs = readout.iqToProbs(data, alg.qubits, correlated=prob_correlated, herald=herald)
            returnValue(np.squeeze(probs).flatten())

    data = sweeps.grid(func, axes, dataset=dataset, save=save, noisy=noisy)

    return data


def calculateReadoutCenters(Sample, measure=0, states=[0, 1], readoutFrequency=None, readoutPower=None,
                            readoutLen=None, control=False, stats=6000, delay=0*ns,
                            update=True, plot=True, save=True, noisy=True):
    sample, devs, qubits, Qubits = gc.loadQubits(Sample, measure, True)
    qc = qubits[0]
    sample1, devs1, qubits1 = gc.loadQubits(Sample, measure=(0,1))
    qt = qubits1[1]
    alg1 = gc.Algorithm(devs1)
    q1 = alg1.q1

    if readoutFrequency is None:
        readoutFrequency = qc['readoutFrequency']
    if readoutPower is None:
        readoutPower = qc['readoutPower']
    if readoutLen is None:
        readoutLen = qc['readoutLen']

    axes = [(range(stats), "Clicks")]
    deps = [[("I", "|%s>" %s, ""), ("Q", "|%s>" %s, "")] for s in states]
    deps = sum(deps, [])
    IQLists = []

    kw = {"stats": stats, 'states': states, 'readoutFrequency': readoutFrequency,
          'readoutPower': readoutPower, 'readoutLen': readoutLen}
    dataset = sweeps.prepDataset(sample, 'calculate Readout Centers', axes, deps, measure=measure, kw=kw)

    with pyle.QubitSequencer() as server:
        for state in states:
            print("Measuring state |%s> " %state)
            alg = gc.Algorithm(devs)
            q0 = alg.q0
            q0['readoutFrequency'] = readoutFrequency
            q0['readoutPower'] = readoutPower
            q0['readoutLen'] = readoutLen
            if control:
                alg1[gates.PiPulse([q1])]
                alg[gates.Wait([q0], 20*ns)]
            alg[gates.MoveToState([q0], 0, state)]
            alg[gates.Wait([q0], delay)]
            alg[gates.Measure([q0])]
            alg.compile()
            data = runQubits(server, alg.agents, stats, dataFormat='iqRaw').wait()
            IQLists.append(np.squeeze(data))
    fids, probs, centers, stds = readout.iqToReadoutFidelity(IQLists, states, k_means=True)

    all_data = [np.array(range(stats)).reshape(-1, 1)]
    [all_data.append(np.squeeze(dat)) for dat in IQLists]
    all_data = np.hstack(all_data)
    all_data = np.array(all_data, dtype='float')

    if save:
        with dataset:
            dataset.add(all_data)

    if noisy:
        for state, fid in zip(states, fids):
            print("|%d> Fidelity: %s" %(state, fid))
        print("Average Fidelity: %s " %np.mean(fids))

    if update:
        Q = Qubits[measure]
        Q["readoutCenterStates"] = centers.keys()
        Q["readoutCenters"] = [round(v.real, 6)+1j*round(v.imag, 6) for v in centers.values()]

    if plot:
        fig = plt.figure(figsize=(6, 4.8))
        ax = fig.add_subplot(1,1,1, aspect='equal')
        for idx, state, color in zip(range(len(states)), states, COLORS):
            IQs = np.squeeze(IQLists[idx])
            center = centers[state]
            ax.plot(IQs[:,0], IQs[:,1], '.', markersize=2, color=color, alpha=0.5, label='|%s>' %state)
            ax.plot([center.real], [center.imag], '*', color='k', zorder=15)
            cir1 = plt.Circle((center.real, center.imag), radius=stds[state], zorder=10,
                             fill=False, fc='k', lw=2, ls='-')
            ax.add_patch(cir1)
            # cir = plt.Circle((center.real, center.imag), radius=stds[state]*2, zorder=5,
            #                   fill=False, fc='k', lw=2, ls='--')
            # ax.add_patch(cir3)
        plt.legend()
        plt.xlabel("I [a.u.]")
        plt.ylabel("Q [a.u.]")

    return fids, centers, stds

if __name__ == '__main__':
    q1 = {'calZpaFunc': [0.16575096, 6.13776594, 1.16224172, 0.24676], 'f10': 5.53834 * GHz, 'Targetfreq':5.33584*GHz}
    q2 = {'calZpaFunc': [0.27626002, 6.15827732, 1.93108506, 0.25], 'f10': 5.08888 * GHz, 'f21': 4.84 * GHz}
    pulse = testCzpulse(q1, q2, t0=0.0 * ns, T=10.0 * ns, G=14 * MHz, thetaf=2.794, N=40001,
                        back=True)
    # func = zfuncs.AmpToFrequency(q1)
    # print func(0.00)
    # func = zfuncs.FrequencyToAmp(q1)
    # print func(5.52176581155)
    # print pulse(60)
    # env.test_env(pulse)
    T = np.linspace(-200, 500, 4001)
    plt.figure()
    plt.plot(T, pulse(T))
    #np.savetxt('tesCZdata.txt',np.vstack((T,pulse(T))).T)
    plt.show()