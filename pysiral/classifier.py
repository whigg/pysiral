# -*- coding: utf-8 -*-
"""
Created on Mon Jul 27 18:09:30 2015

@author: Stefan
"""

import numpy as np


class BaseClassifier(object):

    def __init__(self):
        pass


class CS2OCOGParameter(BaseClassifier):
    """
    Calculate OCOG Parameters (Amplitude, Width) for CryoSat-2 waveform
    counts.
    Algorithm Source: retrack_ocog.pro from CS2AWI lib
    """

    def __init__(self, wfm_counts):
        super(CS2OCOGParameter, self).__init__()
        self._n = np.shape(wfm_counts)[0]
        self._amplitude = np.ndarray(shape=(self._n), dtype=np.float32)
        self._width = np.ndarray(shape=(self._n), dtype=np.float32)
        self._calc_parameters(wfm_counts)

    def _calc_parameters(self, wfm_counts):
        for i in np.arange(self._n):
            y = wfm_counts[i, :].flatten().astype(np.float32)
            y -= np.nanmean(y[0:11])  # Remove Noise
            y[np.where(y < 0.0)[0]] = 0.0  # Set negative counts to zero
            y2 = y**2.0
            self._amplitude[i] = np.sqrt((y2**2.0).sum() / y2.sum())
            self._width[i] = ((y2.sum())**2.0) / (y2**2.0).sum()

    @property
    def amplitude(self):
        return self._amplitude

    @property
    def width(self):
        return self._width


class CS2PulsePeakiness(BaseClassifier):
    """
    Calculates Pulse Peakiness (full, left & right) for CryoSat-2 waveform
    counts
    XXX: This is a 1 to 1 legacy implementation of the IDL CS2AWI method,
         consistent method of L1bData or L2Data is required
    """
    def __init__(self, wfm_counts, pad=2):
        super(CS2PulsePeakiness, self).__init__()
        shape = np.shape(wfm_counts)
        self._n = shape[0]
        self._n_range_bins = shape[1]
        self._pad = pad
        dtype = np.float32
        self._peakiness = np.ndarray(shape=(self._n), dtype=dtype)*np.nan
        self._peakiness_r = np.ndarray(shape=(self._n), dtype=dtype)*np.nan
        self._peakiness_l = np.ndarray(shape=(self._n), dtype=dtype)*np.nan
        self._calc_parameters(wfm_counts)

    def _calc_parameters(self, wfm_counts):
        for i in np.arange(self._n):
            try:
                y = wfm_counts[i, :].flatten().astype(np.float32)
                y -= np.nanmean(y[0:11])  # Remove Noise
                y[np.where(y < 0.0)[0]] = 0.0  # Set negative counts to zero
                yp = np.nanmax(y)  # Waveform peak value
                ypi = np.nanargmax(y)  # Waveform peak index
                if ypi > 3*self._pad and ypi < self._n_range_bins-4*self._pad:
                    self._peakiness_l[i] = yp/np.nanmean(
                        y[ypi-3*self._pad:ypi-1*self._pad+1])*3.0
                    self._peakiness_r[i] = yp/np.nanmean(y[
                        ypi+1*self._pad:ypi+3*self._pad+1])*3.0
                    self._peakiness[i] = yp/y.sum()*self._n_range_bins
            except ValueError:
                self._peakiness_l[i] = np.nan
                self._peakiness_r[i] = np.nan
                self._peakiness[i] = np.nan

    @property
    def peakiness(self):
        return self._peakiness

    @property
    def peakiness_r(self):
        return self._peakiness_r

    @property
    def peakiness_l(self):
        return self._peakiness_l


class EnvisatWaveformParameter(BaseClassifier):
    """
    Currently only computes pulse peakiness for Envisat waveforms
    from SICCI processor.

    Parameter for Envisat from SICCI Processor
        skip = 5
        bins_after_nominal_tracking_bin = 83
    """

    def __init__(self, wfm, skip=5, bins_after_nominal_tracking_bin=83):
        super(EnvisatWaveformParameter, self).__init__()
        self.t_n = bins_after_nominal_tracking_bin
        self.skip = skip
        self._n = wfm.shape[0]
        self._n_range_bins = wfm.shape[1]
        self._init_parameter()
        self._calc_parameter(wfm)

    def _init_parameter(self):
        self.peakiness_old = np.ndarray(shape=(self._n), dtype=np.float32)
        self.peakiness = np.ndarray(shape=(self._n), dtype=np.float32)*np.nan

    def _calc_parameter(self, wfm):
        for i in np.arange(self._n):
            # Discard first bins, they are FFT artefacts anyway
            wave = wfm[i, self.skip:]

            ## old peakiness
            try:
                pp = 0.0 + self.t_n * float(max(wave)) / float(sum(wave))
            except ZeroDivisionError:
                pp = np.nan
            self.peakiness_old[i] = pp

            ## new peakiness
            try:
                self.peakiness[i] = float(max(wave))/float(sum(wave))*self._n_range_bins
            except ZeroDivisionError:
                self.peakiness[i] = np.nan