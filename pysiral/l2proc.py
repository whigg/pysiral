# -*- coding: utf-8 -*-
"""
Created on Fri Jul 24 14:04:27 2015

@author: Stefan
"""

from pysiral.config import (td_branches, ConfigInfo, TimeRangeRequest,
                            get_yaml_config, PYSIRAL_VERSION, HOSTNAME)
from pysiral.errorhandler import ErrorStatus, PYSIRAL_ERROR_CODES
from pysiral.datahandler import DefaultAuxdataHandler
from pysiral.l1bdata import L1bdataNCFile
from pysiral.iotools import get_local_l1bdata_files
from pysiral.l2data import Level2Data
from pysiral.logging import DefaultLoggingClass
from pysiral.mss import get_l2_ssh_class
from pysiral.output import (Level2Output, DefaultLevel2OutputHandler,
                            PysiralOutputFilenaming, get_output_class)
from pysiral.roi import get_roi_class
from pysiral.surface_type import get_surface_type_class
from pysiral.retracker import get_retracker_class
from pysiral.filter import get_filter
from pysiral.validator import get_validator
from pysiral.frb import get_frb_algorithm
from pysiral.sit import get_sit_algorithm
from pysiral.path import filename_from_path, file_basename

from dateutil.relativedelta import relativedelta
from isodate.duration import Duration

from collections import deque, OrderedDict
from datetime import datetime
import time
import glob
import sys
import os


class Level2Processor(DefaultLoggingClass):

    def __init__(self, product_def, auxdata_handler=None):

        super(Level2Processor, self).__init__(self.__class__.__name__)

        # Error Status Handler
        self.error = ErrorStatus(caller_id=self.__class__.__name__)

#        # Level-2 Algorithm Defintion
        self._l2def = product_def.l2def

        # Auxiliary Data Handler
        if auxdata_handler is None:
            auxdata_handler = DefaultAuxdataHandler()
        self._auxdata_handler = auxdata_handler

        # Output_handler (can be one or many)
        self._output_handler = product_def.output_handler

        # List of Level-2 (processed) orbit segments
        self._orbit = deque()

        # List of Level-1b input files
        self._l1b_files = []

        # pysiral config
        self._config = ConfigInfo()

        # Processor Initialization Flag
        self._initialized = False

        # Processor summary report
        self.report = L2ProcessorReport()

        # Initialize the class
        self._initialize_processor()


# %% Level2Processor: class properties

    @property
    def orbit(self):
        return self._orbit

    @property
    def has_empty_file_list(self):
        return len(self._l1b_files) == 0

    @property
    def l2_auxdata_source_dict(self):
        """ A dictionary that contains the descriptions of the auxiliary
        data sources """
        auxdata_dict = {}
        for auxdata_type in ["mss", "sic", "sitype", "snow"]:
            try:
                handler = getattr(self, "_"+auxdata_type)
                auxdata_dict[auxdata_type] = handler.longname
            except AttributeError:
                auxdata_dict[auxdata_type] = "unspecified"
        return auxdata_dict

# %% Level2Processor: public methods

#    def initialize(self):
#        self._initialize_processor()
#        self._initialize_summary_report()

    def get_input_files_local_machine_def(self, time_range, version="default"):
        mission_id = self._l2def.mission_id
        hemisphere = self._l2def.hemisphere
        l1b_files = get_local_l1bdata_files(
            mission_id, time_range, hemisphere, version=version)
        self.set_l1b_files(l1b_files)

        # Update the report
        self.report.n_files = len(l1b_files)
        self.report.time_range = time_range
        self.report.l1b_repository = os.path.split(l1b_files[0])[0]

    def set_custom_l1b_file_list(self, l1b_files, time_range):
        self.set_l1b_files(l1b_files)
        # Update the report
        self.report.n_files = len(l1b_files)
        self.report.time_range = time_range
        self.report.l1b_repository = os.path.split(l1b_files[0])[0]

    def set_l1b_files(self, l1b_files):
        self._l1b_files = l1b_files

    def remove_old_l2data(self, time_range):
        """ Clean up old l2 output data """
        # TODO: Move data management out of processing class
        # can be several oututs
        output_ids, output_defs = td_branches(self._l2def.output)
        for output_id, output_def in zip(output_ids, output_defs):
            output = get_output_class(output_def.pyclass)
            output.set_options(**output_def.options)
            output.set_base_export_path(output_def.path)
            export_folder = output.get_full_export_path(time_range.start)

            # Get list of output files
            search_pattern = os.path.join(export_folder, "*.*")
            l2output_files = glob.glob(search_pattern)

            # Delete files
            self.log.info("Removing %g output files [ %s ] in %s" % (
                len(l2output_files), output_id, export_folder))
            for l2output_file in l2output_files:
                os.remove(l2output_file)

    def process_l1b_files(self, l1b_files):
        self.set_l1b_files(l1b_files)
        self.run()

    def run(self):
        """ Run the processor """
        self._l2_processing_of_orbit_files()
        self._l2proc_summary_to_file()
        self._clean_up()

    def purge(self):
        """ Clean the orbit collection """
        pass

# %% Level2Processor: house keeping methods

    def _l2proc_summary_to_file(self):
        output_ids, output_defs = td_branches(self._l2def.output)
        for output_id, output_def in zip(output_ids, output_defs):
            output = get_output_class(output_def.pyclass)
            output.set_options(**output_def.options)
            output.set_base_export_path(output_def.path)
            time_range = self.report.time_range
            export_folder = output.get_full_export_path(time_range.start)
            self.report.write_to_file(output_id, export_folder)

    def _clean_up(self):
        """ All procedures that need to be reset after a run """
        self.report.clean_up()

# %% Level2Processor: initialization

    def _initialize_processor(self):
        """ Read required auxiliary data sets """

        # Instance can be reused
        if self._initialized:
            # Empty orbit list (or else orbits will acculumate)
            self._orbit.clear()
            return

        self.log.info("Starting Initialization")

        self.log.info("Processor Settings - range correction list:")
        for correction in self._l2def.corrections:
            self.log.info("- %s" % correction)
        self.log.info("Processor Settings - surface type classificator: %s" % (
            self._l2def.surface_type.pyclass))
        self.log.info("Processor Settings - lead interpolator: %s" % (
            self._l2def.ssa.pyclass))

        # Set the region of interest option
        # (required for MSS subsetting)
        self._set_roi()

        # Load static background field

        # Read the mean surface height auxiliary file
        self._set_mss()

        # Handler for dynamic data sets (sea ice concentration, ...)
        # need to be called with timestamps and positions

        # Sea ice concentration data handler
        self._set_sic_handler()

        # sea ice type data handler (needs to be before snow)
        self._set_sitype_handler()

        # snow data handler (needs to provide snow depth and density)
        self._set_snow_handler()

        # Report on output location
        self._report_output_location()

        # All done
        self._initialized = True
        self.log.info("Initialization complete")

    def _set_roi(self):
        self.log.info("Processor Settings - ROI: %s" % self._l2def.roi.pyclass)
        self._roi = get_roi_class(self._l2def.roi.pyclass)
        self._roi.set_options(**self._l2def.roi.options)

    def _set_mss(self):
        """ Loading the mss product file from a static file """
        settings = self._l2def.auxdata.mss
        self._mss = self._auxdata_handler.get_pyclass("mss", settings.name)
        self._auxdata_handler.error.raise_on_error()
        self.log.info("Processor Settings - MSS: %s" % self._mss.pyclass)
        self.log.info("- loading roi subset from: %s" % self._mss.filename)
        self._mss.set_roi(self._roi)
        self._mss.initialize()

    def _set_sic_handler(self):
        """ Set the sea ice concentration handler """
        settings = self._l2def.auxdata.sic
        self._sic = self._auxdata_handler.get_pyclass("sic", settings.name)
        self._auxdata_handler.error.raise_on_error()
        if settings.options is not None:
            self._sic.set_options(**settings.options)
        self._sic.initialize()
        self.log.info("Processor Settings - SIC handler: %s" % (
            self._sic.pyclass))

    def _set_sitype_handler(self):
        """ Set the sea ice type handler """
        settings = self._l2def.auxdata.sitype
        self._sitype = self._auxdata_handler.get_pyclass(
                "sitype", settings.name)
        self._auxdata_handler.error.raise_on_error()
        if settings.options is not None:
            self._sitype.set_options(**settings.options)
        self.log.info("Processor Settings - SIType handler: %s" % (
            self._sitype.pyclass))

    def _set_snow_handler(self):
        """ Set the snow (depth and density) handler """
        settings = self._l2def.auxdata.snow
        self._snow = self._auxdata_handler.get_pyclass("snow", settings.name)
        self._auxdata_handler.error.raise_on_error()
        if settings.options is not None:
            self._snow.set_options(**settings.options)
        self.log.info("Processor Settings - Snow handler: %s" % (
            self._snow.pyclass))

    def _report_output_location(self):
        for output_handler in self._output_handler:
            msg = "Level-2 Output [%s]: %s" % (str(output_handler.id), output_handler.basedir)
            self.log.info(msg)

    def _initialize_summary_report(self):
        """
        Only add report parameter that are not time range specific
        (e.g. the processor l2 settings)
        """
        self.report.l2_settings_file = self._l2def.l2_settings_file

# %% Level2Processor: orbit processing

    def _l2_processing_of_orbit_files(self):
        """ Orbit-wise level2 processing """
        # TODO: Evaluate parallelization
        self.log.info("Start Orbit Processing")

        # loop over l1bdata preprocessed orbits
        for i, l1b_file in enumerate(self._l1b_files):

            # Log the current position in the file stack
            self.log.info("+ [ %g of %g ] (%.2f%%)" % (
                i+1, len(self._l1b_files),
                float(i+1)/float(len(self._l1b_files))*100.))

            # Read the the level 1b file (l1bdata netCDF is required)
            l1b = self._read_l1b_file(l1b_file)
            source_primary_filename = os.path.split(l1b_file)[-1]

            # Apply the geophysical range corrections on the waveform range
            # bins in the l1b data container
            # TODO: move to level1bData class
            self._apply_range_corrections(l1b)

            # Apply a pre-filter of the l1b data (can be none)
            self._apply_l1b_prefilter(l1b)

            # Initialize the orbit level-2 data container
            try:
                time_range = TimeRangeRequest(l1b.info.start_time,
                                              l1b.info.stop_time,
                                              period="custom")
                period = time_range.iterations[0]
            except SystemExit:
                msg = "Computation of data period caused exception"
                self.log.warning("[invalid-l1b]", msg)
                continue

            l2 = Level2Data(l1b.info, l1b.time_orbit, period=period)

            # Add sea ice concentration (can be used as classifier)
            error_status, error_codes = self._get_sea_ice_concentration(l2)
            if error_status:
                self._discard_l1b_procedure(error_codes, l1b_file)
                continue

            # Get sea ice type (may be required for geometrical corrcetion)
            error_status, error_codes = self._get_sea_ice_type(l2)
            if error_status:
                self._discard_l1b_procedure(error_codes, l1b_file)
                continue

            # get mss for orbit (this is necessary e.g. for icesat)
            l2.mss = self._mss.get_track(l2.track.longitude, l2.track.latitude)

            # Surface type classification (ocean, ice, lead, ...)
            # (ice type classification comes later)
            # TODO: Add L2 classifiers (ice concentration, ice type)
            self._classify_surface_types(l1b, l2)

            # Validate surface type classification
            # yes/no decision on continuing with orbit
            error_status, error_codes = self._validate_surface_types(l2)
            if error_status:
                self._discard_l1b_procedure(error_codes, l1b_file)
                continue

            # Get elevation by retracking of different surface types
            # adds parameter elevation to l2
            error_status, error_codes = self._waveform_retracking(l1b, l2)
            if error_status:
                self._discard_l1b_procedure(error_codes, l1b_file)
                continue

            # Compute the sea surface anomaly (from mss and lead tie points)
            # adds parameter ssh, ssa, afrb to l2
            self._estimate_sea_surface_height(l2)

            # Compute the radar freeboard and its uncertainty
            self._get_altimeter_freeboard(l1b, l2)

            # Get snow depth & density
            error_status, error_codes = self._get_snow_parameters(l2)
            if error_status:
                self.report.add_orbit_discarded_event(error_codes, l1b_file)
                continue

            # get radar(-derived) from altimeter freeboard
            self._get_freeboard_from_radar_freeboard(l1b, l2)

            # Apply freeboard filter
            self._apply_freeboard_filter(l2)

            # Convert to thickness
            self._convert_freeboard_to_thickness(l2)

            # Filter thickness
            self._apply_thickness_filter(l2)

            # Create output files
            l2.set_metadata(auxdata_source_dict=self.l2_auxdata_source_dict,
                            source_primary_filename=source_primary_filename,
                            l2_algorithm_id=self._l2def.id,
                            l2_version_tag=self._l2def.version_tag)
            self._create_l2_outputs(l2)

            # Add data to orbit stack
            self._add_to_orbit_collection(l2)

    def _read_l1b_file(self, l1b_file):
        """ Read a L1b data file (l1bdata netCDF) """
        filename = filename_from_path(l1b_file)
        self.log.info("- Parsing l1bdata file: %s" % filename)
        l1b = L1bdataNCFile(l1b_file)
        l1b.parse()
        l1b.info.subset_region_name = self._l2def.roi.hemisphere
        return l1b

    def _discard_l1b_procedure(self, error_codes, l1b_file):
        """ Log and report discarded l1b orbit segment """
        self.log.info("- skip file")
        for error_code in error_codes:
            self.report.add_orbit_discarded_event(error_code, l1b_file)

    def _apply_range_corrections(self, l1b):
        """ Apply the range corrections """
        # XXX: This should be applied to the L2 data not l1b
        for correction in self._l2def.corrections:
            l1b.apply_range_correction(correction)

    def _apply_l1b_prefilter(self, l1b):
        """ Apply filtering of l1b variables """
        # Backward compatibility with older l2 setting files
        if "l1b_pre_filtering" not in self._l2def:
            return
        # Apply filters
        names, filters = td_branches(self._l2def.l1b_pre_filtering)
        for name, filter_def in zip(names, filters):
            self.log.info("- Apply l1b pre-filter: %s" % filter_def.pyclass)
            l1bfilter = get_filter(filter_def.pyclass)
            l1bfilter.set_options(**filter_def.options)
            l1bfilter.apply_filter(l1b)

    def _get_sea_ice_concentration(self, l2):
        """ Get sea ice concentration along track from auxdata """

        # Get along-track sea ice concentrations via the SIC handler class
        # (see self._set_sic_handler)
        sic, msg = self._sic.get_along_track_sic(l2)

        # Report any messages from the SIC handler
        if not msg == "":
            self.log.info("- "+msg)

        # Check and return error status and codes (e.g. missing file)
        error_status = self._sic.error.status
        error_codes = self._sic.error.codes

        # No error: Set sea ice concentration data to the l2 data container
        if not error_status:
            l2.sic.set_value(sic)

        # on error: display error messages as warning and return status flag
        # (this will cause the processor to report and skip this orbit segment)
        else:
            error_messages = self._sic.error.get_all_messages()
            for error_message in error_messages:
                self.log.warning("! "+error_message)
                # SIC Handler is persistent, therefore errors status
                # needs to be reset before next orbit
                self._sic.error.reset()

        return error_status, error_codes

    def _get_sea_ice_type(self, l2):
        """ Get sea ice type (myi fraction) along track from auxdata """

        # Call the sitype handler
        sitype, sitype_unc, msg = self._sitype.get_along_track_sitype(l2)

        # Report any messages from the sitype handler
        if not msg == "":
            self.log.info("- "+msg)

        # Check and return error status and codes (e.g. missing file)
        error_status = self._sitype.error.status
        error_codes = self._sitype.error.codes

        # Add to l2data
        if not error_status:
            l2.sitype.set_value(sitype)
            l2.sitype.set_uncertainty(sitype_unc)

        # on error: display error messages as warning and return status flag
        # (this will cause the processor to report and skip this orbit segment)
        else:
            error_messages = self._sitype.error.get_all_messages()
            for error_message in error_messages:
                self.log.warning("! "+error_message)
                # SIC Handler is persistent, therefore errors status
                # needs to be reset before next orbit
            self._sitype.error.reset()

        return error_status, error_codes

    def _classify_surface_types(self, l1b, l2):
        """ Run the surface type classification """
        pyclass = self._l2def.surface_type.pyclass
        surface_type = get_surface_type_class(pyclass)
        surface_type.set_options(**self._l2def.surface_type.options)
        surface_type.classify(l1b, l2)
        l2.set_surface_type(surface_type.result)

    def _validate_surface_types(self, l2):
        """ Loop over stack of surface type validators """
        surface_type_validators = self._l2def.validator.surface_type
        names, validators = td_branches(surface_type_validators)
        error_codes = ["l2proc_surface_type_discarded"]
        error_states = []
        error_messages = []
        for name, validator_def in zip(names, validators):
            validator = get_validator(validator_def.pyclass)
            validator.set_options(**validator_def.options)
            state, message = validator.validate(l2)
            error_states.append(state)
            error_messages.append(message)
            if state:
                self.log.info("- Validator message: "+message)
        error_status = True in error_states
        return error_status, error_codes

    def _waveform_retracking(self, l1b, l2):
        """ Retracking: Obtain surface elevation from l1b waveforms """
        # loop over retrackers for each surface type
        surface_types, retracker_def = td_branches(self._l2def.retracker)

        for i, surface_type in enumerate(surface_types):

            # Check if any waveforms need to be retracked for given
            # surface type
            surface_type_flag = l2.surface_type.get_by_name(surface_type)
            if surface_type_flag.num == 0:
                self.log.info("- no waveforms of type %s" % surface_type)
                continue

            # Benchmark retracker performance
            # XXX: is currently the bottleneck of level2 processing
            timestamp = time.time()

            # Retrieve the retracker assiciated with surface type
            # from the l2 settings
            retracker = get_retracker_class(retracker_def[i].pyclass)

            # Set options (if any)
            if retracker_def[i].options is not None:
                retracker.set_options(**retracker_def[i].options)

            # set subset of waveforms
            retracker.set_indices(surface_type_flag.indices)

            # Add classifier data (some retracker need that)
            retracker.set_classifier(l1b.classifier)

            # Start the retracking
            retracker.retrack(l1b, l2)

            # Retrieve the range after retracking
            l2.update_retracked_range(retracker)

            # XXX: Let the retracker return other parameters?
            l2.set_radar_mode(l1b.waveform.radar_mode)

            # retrieve potential error status and update surface type flag
            if retracker.error_flag.num > 0:
                l2.surface_type.add_flag(retracker.error_flag.flag, "invalid")
            self.log.info("- Retrack class %s with %s in %.3f seconds" % (
                surface_type, retracker_def[i].pyclass,
                time.time()-timestamp))

        # Error handling not yet implemented, return dummy values
        return False, None

    def _estimate_sea_surface_height(self, l2):

        # 2. get get sea surface anomaly
        ssa = get_l2_ssh_class(self._l2def.ssa.pyclass)
        ssa.set_options(**self._l2def.ssa.options)
        ssa.interpolate(l2)

        # dedicated setters, else the uncertainty, bias attributes are broken
        l2.ssa.set_value(ssa.value)
        l2.ssa.set_uncertainty(ssa.uncertainty)

    def _get_altimeter_freeboard(self, l1b, l2):
        """ Compute radar freeboard and its uncertainty """

        afrbalg = get_frb_algorithm(self._l2def.afrb.pyclass)
        afrbalg.set_options(**self._l2def.rfrb.options)
        afrb, afrb_unc = afrbalg.get_radar_freeboard(l1b, l2)

        # Check and return error status and codes
        # (unlikely in this case)
        error_status = afrbalg.error.status
        error_codes = afrbalg.error.codes

        if not error_status:
            # Add to l2data
            l2.afrb.set_value(afrb)
            l2.afrb.set_uncertainty(afrb_unc)

        # on error: display error messages as warning and return status flag
        # (this will cause the processor to report and skip this orbit segment)
        else:
            error_messages = self._snow.error.get_all_messages()
            for error_message in error_messages:
                self.log.warning("! "+error_message)

        return error_status, error_codes

    def _get_snow_parameters(self, l2):
        """ Get snow depth and density with respective uncertainties """

        # Get along track snow depth info
        snow, msg = self._snow.get_along_track_snow(l2)

        # Report any messages from the snow handler
        if not msg == "":
            self.log.info("- "+msg)

        # Check and return error status and codes (e.g. missing file)
        error_status = self._snow.error.status
        error_codes = self._snow.error.codes

        # Add to l2data
        if not error_status:
            # Add to l2data
            l2.snow_depth.set_value(snow.depth)
            l2.snow_depth.set_uncertainty(snow.depth_uncertainty)
            l2.snow_dens.set_value(snow.density)
            l2.snow_dens.set_uncertainty(snow.density_uncertainty)

        # on error: display error messages as warning and return status flag
        # (this will cause the processor to report and skip this orbit segment)
        else:
            error_messages = self._snow.error.get_all_messages()
            for error_message in error_messages:
                self.log.warning("! "+error_message)
                # SIC Handler is persistent, therefore errors status
                # needs to be reset before next orbit
            self._snow.error.reset()

        return error_status, error_codes

    def _get_freeboard_from_radar_freeboard(self, l1b, l2):
        """ Convert the altimeter freeboard in radar freeboard """

        frbgeocorr = get_frb_algorithm(self._l2def.frb.pyclass)
        frbgeocorr.set_options(**self._l2def.frb.options)
        frb, frb_unc = frbgeocorr.get_freeboard(l1b, l2)

        # Check and return error status and codes (e.g. missing file)
        error_status = frbgeocorr.error.status
        error_codes = frbgeocorr.error.codes

        # Add to l2data
        if not error_status:
            # Add to l2data
            l2.frb.set_value(frb)
            l2.frb.set_uncertainty(frb_unc)

        # on error: display error messages as warning and return status flag
        # (this will cause the processor to report and skip this orbit segment)
        else:
            error_messages = frbgeocorr.get_all_messages()
            for error_message in error_messages:
                self.log.warning("! "+error_message)

    def _apply_freeboard_filter(self, l2):
        """ Apply freeboard filters as defined in the level-2 settings file
        under `root.filter.freeboard`

        Filtering means:
        - setting the freeboard value to nan
        - setting the surface type classification to invalid
        """

        # Extract filters from settings structure
        freeboard_filters = self._l2def.filter.freeboard
        names, filters = td_branches(freeboard_filters)

        # Loop over freeboard filters
        for name, filter_def in zip(names, filters):

            # Get corresponding class name in pysiral.filter and transfer options
            # XXX: This should be rewritten as (e.g.)
            #   `frbfilter = VariableFilter(filter_def.pyclass, **filter_def.options)`
            frbfilter = get_filter(filter_def.pyclass)
            frbfilter.set_options(**filter_def.options)

            # XXX: This is a temporary fix of an error in the algorithm
            #
            # Explanation: The filter target was wrongly set to radar freeboard,
            # meaning that whether a freeboard value was filtered was determined on
            # the wrong parameter. Both values differ by the geometric snow propagation
            # correction (22% of snow depth). While the impact on the high freeboard end
            # is negligible, at the lower (negative) end more freeboard where filtered
            # than necessary since radar freeboard is always lower.
            #
            # The `afrb` filter target was hard coded, thus an option is added to replace
            # the filter target (`root.filter.freeboard.frb_valid_range.filter_target`).
            # The default option is the wrong one only for consistency reasons.
            filter_target = "afrb"
            if filter_def.options.has_key("filter_target"):
                filter_target = filter_def.options.filter_target

            # Check if action is required
            frbfilter.apply_filter(l2, filter_target)
            if frbfilter.flag.num == 0:
                continue

            # Logging
            self.log.info("- Filter message: %s has flagged %g waveforms" % (
                filter_def.pyclass, frbfilter.flag.num))

            # Set surface type flag (contains invalid)
            l2.surface_type.add_flag(frbfilter.flag.flag, "invalid")

            # Remove invalid elevations / freeboards
            l2.frb.set_nan_indices(frbfilter.flag.indices)

    def _convert_freeboard_to_thickness(self, l2):
        """
        Convert Freeboard to Thickness
        Note: This step requires the definition of sea ice density
              (usually in the l2 settings)
        """

        frb2sit = get_sit_algorithm(self._l2def.sit.pyclass)
        frb2sit.set_options(**self._l2def.sit.options)

        sit, sit_unc, ice_dens, ice_dens_unc = frb2sit.get_thickness(l2)

        # Check and return error status and codes (e.g. missing file)
        error_status = frb2sit.error.status

        # Add to l2data
        if not error_status:
            # Add to l2data
            l2.sit.set_value(sit)
            l2.sit.set_uncertainty(sit_unc)
            l2.ice_dens.set_value(ice_dens)
            l2.ice_dens.set_uncertainty(ice_dens_unc)

    def _apply_thickness_filter(self, l2):
        thickness_filters = self._l2def.filter.thickness
        names, filters = td_branches(thickness_filters)
        for name, filter_def in zip(names, filters):
            sitfilter = get_filter(filter_def.pyclass)
            sitfilter.set_options(**filter_def.options)
            sitfilter.apply_filter(l2, "sit")
            if sitfilter.flag.num == 0:
                continue
            self.log.info("- Filter message: %s has flagged %g waveforms" % (
                filter_def.pyclass, sitfilter.flag.num))
            # Set surface type flag (contains invalid)
            l2.surface_type.add_flag(sitfilter.flag.flag, "invalid")
            # Remove invalid thickness values
            l2.sit.set_nan_indices(sitfilter.flag.indices)

    def _create_l2_outputs(self, l2):
        for output_handler in self._output_handler:
            output = Level2Output(l2, output_handler)
            self.log.info("- Write %s data file: %s" % (
                    output_handler.id, output.export_filename))

    def _add_to_orbit_collection(self, l2):
        self._orbit.append(l2)


class Level2ProductDefinition(DefaultLoggingClass):
    """ Main configuration class for the Level-2 Processor """

    def __init__(self, run_tag, l2_settings_file):

        super(Level2ProductDefinition, self).__init__(self.__class__.__name__)
        self.error = ErrorStatus(self.__class__.__name__)

        # Mandatory parameter
        self._run_tag = run_tag
        self._l2_settings_file = l2_settings_file
        self._parse_l2_settings()

        # Optional parameters (may be set to default values if not specified)
        self._output_handler = []

    def add_output_definition(self, output_def_file,
                              period="default",
                              overwrite_protection=True):

        # Set given or default output handler
        self._output_handler.append(DefaultLevel2OutputHandler(
            output_def=output_def_file, subdirectory=self.run_tag,
            period=period, overwrite_protection=overwrite_protection))

    def _parse_l2_settings(self):
        try:
            self._l2def = get_yaml_config(self._l2_settings_file)
        except Exception, msg:
            self.error.add_error("invalid-l2-settings", msg)
            self.error.raise_on_error()

    @property
    def run_tag(self):
        return self._run_tag

    @property
    def l2def(self):
        return self._l2def

    @property
    def output_handler(self):
        # Revert to default output handler if non was specifically added
        if len(self._output_handler) == 0:
            self.add_output_definition("default")
        return self._output_handler


class L2ProcessorReport(DefaultLoggingClass):

    def __init__(self):

        super(L2ProcessorReport, self).__init__(self.__class__.__name__)

        self.n_files = 0
        self.data_period = None
        self.l2_settings_file = "none"
        self.l1b_repository = "none"

        # Counter for error codes
        # XXX: This is a first quick implementation of error codes
        #      (see pysiral.error_handler modules for more info) and
        #      the dev should make sure to use the correct names. A
        #      more formalized way of reporting errors will be added
        #      in future updates
        self._init_error_counters()

    def add_orbit_discarded_event(self, error_code, l1b_file):
        """ Add the l1b file to the list of files with a certain error code """

        # Only except defined error codes
        try:
            self.error_counter[error_code].append(l1b_file)
        except:
            self.log.warning("Unknown error code (%s), ignoring" % error_code)

    def write_to_file(self, output_id, directory):
        """ Write a summary file to the defined export directory """

        # Create a simple filename
        filename = os.path.join(directory, "pysiral-l2proc-summary.txt")
        self.log.info("Exporting summary report: %s" % filename)

        lfmt = "  %-16s : %s\n"
        current_time = str(datetime.now()).split(".")[0]
        with open(filename, "w") as fhandle:

            # Write infos on settings, host, os, ....
            fhandle.write("# pysiral Level2Processor Summary\n\n")
            fhandle.write(lfmt % ("created", current_time))

            # Brief statistics of files, errors, warnings
            fhandle.write("\n# Processor Statistics\n\n")
            fhandle.write(lfmt % ("l1b files", str(self.n_files)))
            fhandle.write(lfmt % ("errors", str(self.n_discarded_files)))
            fhandle.write(lfmt % ("warnings", str(self.n_warnings)))

            fhandle.write("\n# Processor & Local Machine Settings\n\n")
            fhandle.write(lfmt % ("pysiral version", PYSIRAL_VERSION))
            fhandle.write(lfmt % ("python version", sys.version))
            fhandle.write(lfmt % ("hostname", HOSTNAME))

            # More info on this specific run
            fhandle.write(lfmt % ("data period", self.data_period_str))
            fhandle.write(lfmt % ("Level-2 settings", self.l2_settings_file))
            fhandle.write(lfmt % ("l1b repository", self.l1b_repository))

            # List discarded files and reason (error code & description)
            fhandle.write("\n# Detailed Error Breakdown\n\n")
            msg = "  No %s output generated for %g l1b files due " + \
                  "to following errors:\n"
            fhandle.write(msg % (output_id, self.n_discarded_files))

            for error_code in PYSIRAL_ERROR_CODES.keys():
                n_discarded_files = len(self.error_counter[error_code])
                if n_discarded_files == 0:
                    continue
                error_description = PYSIRAL_ERROR_CODES[error_code]
                msg = "\n  %g file(s): [error_code:%s] %s\n" % (
                    n_discarded_files, error_code, error_description)
                fhandle.write(msg)
                for discarded_file in self.error_counter[error_code]:
                    fn = filename_from_path(discarded_file)
                    fhandle.write("  * %s\n" % fn)

    def clean_up(self):
        """ Remove all non-persistent parameter """
        self.data_period = None
        self.l1b_repository = "none"
        self._init_error_counters()

    def _init_error_counters(self):
        self.error_counter = OrderedDict([])
        for error_code in PYSIRAL_ERROR_CODES.keys():
            self.error_counter[error_code] = []

    @property
    def data_period_str(self):
        try:
            return self.time_range.label
        except:
            return "invalid/mission data period"

    @property
    def n_discarded_files(self):
        num_discarded_files = 0
        for error_code in self.error_counter.keys():
            num_discarded_files += len(self.error_counter[error_code])
        return num_discarded_files

    @property
    def n_warnings(self):
        return 0
