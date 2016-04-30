# -*- coding: utf-8 -*-
"""
Created on Thu Jul 23 15:10:04 2015

@author: Stefan
"""

from pysiral.cryosat2.functions import (
    tai2utc, get_tai_datetime_from_timestamp,
    get_cryosat2_wfm_power, get_cryosat2_wfm_range)
from pysiral.esa.functions import get_structarr_attr
from pysiral.cryosat2.l1bfile import CryoSatL1B
from pysiral.envisat.sgdrfile import EnvisatSGDR
from pysiral.helper import parse_datetime_str
from pysiral.classifier import (CS2OCOGParameter, CS2PulsePeakiness,
                                EnvisatWaveformParameter)

import numpy as np
# import time

ESA_SURFACE_TYPE_DICT = {
    "ocean": 0,
    "closed_sea": 1,
    "land_ice": 2,
    "land": 3}


class L1bAdapterCryoSat(object):
    """ Converts a CryoSat2 L1b object into a L1bData object """

    def __init__(self, config):
        self.filename = None
        self._config = config
        self._mission = "cryosat2"

    def construct_l1b(self, l1b, header_only=False):
        self.l1b = l1b                        # pointer to L1bData object
        self._read_cryosat2l1b_header()       # Read CryoSat-2 L1b header
        if not header_only:
            self.read_msd()

    def read_msd(self):
        self._read_cryosat2l1b_data()         # Read CryoSat-2 data content
        self._transfer_metadata()             # (orbit, radar mode, ..)
        self._transfer_timeorbit()            # (lon, lat, alt, time)
        self._transfer_waveform_collection()  # (power, range)
        self._transfer_range_corrections()    # (range corrections)
        self._transfer_surface_type_data()    # (land flag, ocean flag, ...)
        self._transfer_classifiers()          # (beam parameters, flags, ...)

    def _read_cryosat2l1b_header(self):
        """
        Read the header and L1b file and stores information
        on open ocean coverage and geographical location in the metadata
        file
        """

        self.cs2l1b = CryoSatL1B()
        self.cs2l1b.filename = self.filename
        self.cs2l1b.parse_header()

        # Populate metadata object with key information to decide if
        # l1b file contains sea ice information

        # open ocean percent from speficic product header in *.DBL
        self.l1b.info.set_attribute(
            "open_ocean_percent", self.cs2l1b.sph.open_ocean_percent*(10.**-2))

        # Geographical coverage of data from start/stop positions in
        # specific product header in *.DBL
        start_lat = self.cs2l1b.sph.start_lat*(10.**-6)
        stop_lat = self.cs2l1b.sph.stop_lat*(10.**-6)
        start_lon = self.cs2l1b.sph.start_long*(10.**-6)
        stop_lon = self.cs2l1b.sph.stop_long*(10.**-6)
        self.l1b.info.set_attribute("lat_min", np.amin([start_lat, stop_lat]))
        self.l1b.info.set_attribute("lat_max", np.amax([start_lat, stop_lat]))
        self.l1b.info.set_attribute("lon_min", np.amin([start_lon, stop_lon]))
        self.l1b.info.set_attribute("lon_max", np.amax([start_lon, stop_lon]))

        error_status = self.cs2l1b.get_status()
        if error_status:
            # TODO: Needs ErrorHandler
            raise IOError()

    def _read_cryosat2l1b_data(self):
        """ Read the L1b file and create a CryoSat-2 native L1b object """
        self.cs2l1b.parse_mds()
        error_status = self.cs2l1b.get_status()
        if error_status:
            # TODO: Needs ErrorHandler
            raise IOError()
        self.cs2l1b.post_processing()

    def _transfer_metadata(self):
        self.l1b.info.mission = self._mission
        self.l1b.info.mission_data_version = self.cs2l1b.baseline
        self.l1b.info.radar_mode = self.cs2l1b.radar_mode
        self.l1b.info.orbit = self.cs2l1b.sph.abs_orbit_start
        self.l1b.info.start_time = parse_datetime_str(
            self.cs2l1b.sph.start_record_tai_time)
        self.l1b.info.stop_time = parse_datetime_str(
            self.cs2l1b.sph.stop_record_tai_time)

    def _transfer_timeorbit(self):
        # Transfer the orbit position
        longitude = get_structarr_attr(self.cs2l1b.time_orbit, "longitude")
        latitude = get_structarr_attr(self.cs2l1b.time_orbit, "latitude")
        altitude = get_structarr_attr(self.cs2l1b.time_orbit, "altitude")
        self.l1b.time_orbit.set_position(longitude, latitude, altitude)
        # Transfer the timestamp
        tai_objects = get_structarr_attr(
            self.cs2l1b.time_orbit, "tai_timestamp")
        tai_timestamp = get_tai_datetime_from_timestamp(tai_objects)
        utc_timestamp = tai2utc(tai_timestamp)
        self.l1b.time_orbit.timestamp = utc_timestamp
        # Update meta data container
        self.l1b.update_data_limit_attributes()

    def _transfer_waveform_collection(self):
        # Create the numpy arrays for power & range
        dtype = np.float32
        n_records = len(self.cs2l1b.waveform)
        n_range_bins = len(self.cs2l1b.waveform[0].wfm)
        echo_power = np.ndarray(shape=(n_records, n_range_bins), dtype=dtype)
        echo_range = np.ndarray(shape=(n_records, n_range_bins), dtype=dtype)
        # Set the echo power in dB and calculate range
        # XXX: This might need to be switchable
        for i, record in enumerate(self.cs2l1b.waveform):
            echo_power[i, :] = get_cryosat2_wfm_power(
                np.array(record.wfm).astype(np.float32),
                record.linear_scale, record.power_scale)
            echo_range[i, :] = get_cryosat2_wfm_range(
                self.cs2l1b.measurement[i].window_delay, n_range_bins)
        # Transfer to L1bData
        self.l1b.waveform.set_waveform_data(
            echo_power, echo_range, self.cs2l1b.radar_mode)

    def _transfer_range_corrections(self):
        # Transfer all the correction in the list
        for key in self.cs2l1b.corrections[0].keys():
            if key in self._config.parameter.correction_list:
                self.l1b.correction.set_parameter(
                    key, get_structarr_attr(self.cs2l1b.corrections, key))
        # CryoSat-2 specific: Two different sources of ionospheric corrections
        options = self._config.get_mission_defaults(self._mission)
        key = options["ionospheric_correction_source"]
        ionospheric = get_structarr_attr(self.cs2l1b.corrections, key)
        self.l1b.correction.set_parameter("ionospheric", ionospheric)

    def _transfer_surface_type_data(self):
        # L1b surface type flag word
        surface_type = get_structarr_attr(
            self.cs2l1b.corrections, "surface_type")
        for key in ESA_SURFACE_TYPE_DICT.keys():
            flag = surface_type == ESA_SURFACE_TYPE_DICT[key]
            self.l1b.surface_type.add_flag(flag, key)

    def _transfer_classifiers(self):
        # Add L1b beam parameter group
        beam_parameter_list = [
            "stack_standard_deviation", "stack_centre",
            "stack_scaled_amplitude", "stack_skewness", "stack_kurtosis"]
        for beam_parameter_name in beam_parameter_list:
            recs = get_structarr_attr(self.cs2l1b.waveform, "beam")
            beam_parameter = [rec[beam_parameter_name] for rec in recs]
            self.l1b.classifier.add(beam_parameter, beam_parameter_name)
        # Calculate Parameters from waveform counts
        # XXX: This is a legacy of the CS2AWI IDL processor
        #      Threshold defined for waveform counts not power in dB
        wfm = get_structarr_attr(self.cs2l1b.waveform, "wfm")
        # Calculate the OCOG Parameter (CryoSat-2 notation)
        ocog = CS2OCOGParameter(wfm)
        self.l1b.classifier.add(ocog.width, "ocog_width")
        self.l1b.classifier.add(ocog.amplitude, "ocog_amplitude")
        # Calculate the Peakiness (CryoSat-2 notation)
        pulse = CS2PulsePeakiness(wfm)
        self.l1b.classifier.add(pulse.peakiness, "peakiness")
        self.l1b.classifier.add(pulse.peakiness_r, "peakiness_r")
        self.l1b.classifier.add(pulse.peakiness_l, "peakiness_l")


class L1bAdapterEnvisat(object):
    """ Converts a Envisat SGDR object into a L1bData object """

    def __init__(self, config):
        self.filename = None
        self._config = config
        self._mission = "envisat"

    def construct_l1b(self, l1b):
        """
        Read the Envisat SGDR file and transfers its content to a
        Level1bData instance
        """
        self.l1b = l1b                        # pointer to L1bData object
        # t0 = time.time()
        self._read_envisat_sgdr()             # Read Envisat SGDR data file
        # t1 = time.time()
        # print "Parsing Envisat SGDR file in %.3g seconds" % (t1 - t0)
        self._transfer_metadata()             # (orbit, radar mode, ..)
        self._transfer_timeorbit()            # (lon, lat, alt, time)
        self._transfer_waveform_collection()  # (power, range)
        self._transfer_range_corrections()    # (range corrections)
        self._transfer_surface_type_data()    # (land flag, ocean flag, ...)
        self._transfer_classifiers()          # (beam parameters, flags, ...)

    def _read_envisat_sgdr(self):
        """ Read the L1b file and create a CryoSat-2 native L1b object """
        self.sgdr = EnvisatSGDR()
        self.sgdr.filename = self.filename
        self.sgdr.parse()
        error_status = self.sgdr.get_status()
        if error_status:
            # TODO: Needs ErrorHandler
            raise IOError()
        self.sgdr.post_processing()

    def _transfer_metadata(self):
        """ Extract essential metadata information from SGDR file """
        info = self.l1b.info
        sgdr = self.sgdr
        info.set_attribute("mission", self._mission)
        info.set_attribute("mission_data_version", "final v9.3p5")
        info.set_attribute("orbit", sgdr.mph.abs_orbit)
        info.set_attribute("cycle", sgdr.mph.cycle)
        info.set_attribute("cycle", sgdr.mph.cycle)
        info.set_attribute("mission_data_source", sgdr.mph.product)

    def _transfer_timeorbit(self):
        """ Extracts the time/orbit data group from the SGDR data """
        # Transfer the orbit position
        self.l1b.time_orbit.set_position(
            self.sgdr.mds_18hz.longitude,
            self.sgdr.mds_18hz.latitude,
            self.sgdr.mds_18hz.altitude)
        # Transfer the timestamp
        self.l1b.time_orbit.timestamp = self.sgdr.mds_18hz.timestamp
        # Update meta data container
        self.l1b.update_data_limit_attributes()

    def _transfer_waveform_collection(self):
        """ Transfers the waveform data (power & range for each range bin) """
        from pysiral.flag import ANDCondition
        # Transfer the reformed 18Hz waveforms
        self.l1b.waveform.set_waveform_data(
            self.sgdr.mds_18hz.power,
            self.sgdr.mds_18hz.range,
            self.sgdr.radar_mode)
        # This is from SICCI-1 processor, check of mcd flags
        valid = ANDCondition()
        valid.add(np.logical_not(self.sgdr.mds_18hz.flag_packet_length_error))
        valid.add(np.logical_not(self.sgdr.mds_18hz.flag_obdh_invalid))
        valid.add(np.logical_not(self.sgdr.mds_18hz.flag_agc_fault))
        valid.add(np.logical_not(self.sgdr.mds_18hz.flag_rx_delay_fault))
        valid.add(np.logical_not(self.sgdr.mds_18hz.flag_waveform_fault))
        # ku_chirp_band_id (0 -> 320Mhz)
        valid.add(self.sgdr.mds_18hz.ku_chirp_band_id == 0)
        self.l1b.waveform.set_valid_flag(valid.flag)

    def _transfer_range_corrections(self):
        # Transfer all the correction in the list
        mds = self.sgdr.mds_18hz
        for correction_name in mds.sgdr_geophysical_correction_list:
            if correction_name not in self._config.parameter.correction_list:
                continue
            self.l1b.correction.set_parameter(
                    correction_name, getattr(mds, correction_name))
        # Envisat specific: There are several options for sources
        #   of geophysical correction in the SGDR files. Selct those
        #   specified in the mission defaults
        #   (see config/mission_def.yaml)
        mission_defaults = self._config.get_mission_options(self._mission)
        correction_options = mission_defaults.geophysical_corrections
        for option in correction_options.iterbranches():
            self.l1b.correction.set_parameter(
                    option.target, getattr(mds, option.selection))

    def _transfer_surface_type_data(self):
        surface_type = self.sgdr.mds_18hz.surface_type
        for key in ESA_SURFACE_TYPE_DICT.keys():
            flag = surface_type == ESA_SURFACE_TYPE_DICT[key]
            self.l1b.surface_type.add_flag(flag, key)

    def _transfer_classifiers(self):
        """
        OCOC retracker parameters are needed for surface type classification
        in Envisat L2 processing
        """
        wfm = self.sgdr.mds_18hz.power
        parameter = EnvisatWaveformParameter(wfm)
        self.l1b.classifier.add(parameter.pulse_peakiness, "pulse_peakiness")
        sea_ice_backscatter = self.sgdr.mds_18hz.sea_ice_backscatter
        self.l1b.classifier.add(sea_ice_backscatter, "sea_ice_backscatter")
