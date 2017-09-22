import time
import logging
import numpy as np
import copy

import os
from pycqed.measurement.waveform_control_CC import qasm_compiler as qcx
from scipy.optimize import brent
from pycqed.measurement.optimization import nelder_mead
import pygsti

from .qubit_object import Qubit
from qcodes.utils import validators as vals
from qcodes.instrument.parameter import ManualParameter, InstrumentRefParameter
from pycqed.measurement.waveform_control_CC import waveform as wf
from pycqed.analysis import measurement_analysis as ma
from pycqed.analysis_v2 import measurement_analysis as ma2
import pycqed.analysis.analysis_toolbox as a_tools

from pycqed.analysis.tools.data_manipulation import rotation_matrix
from pycqed.measurement.calibration_toolbox import (
    mixer_carrier_cancellation, mixer_skewness_calibration_CBoxV3)

from pycqed.measurement import sweep_functions as swf
from pycqed.measurement.waveform_control_CC import single_qubit_qasm_seqs as sqqs
import pycqed.measurement.CBox_sweep_functions as cbs
from pycqed.measurement.waveform_control_CC import qasm_helpers as qh
from pycqed.measurement.waveform_control_CC import qasm_to_asm as qta
from pycqed.measurement.waveform_control_CC import instruction_lib as ins_lib

from pycqed.measurement.waveform_control_CC import QWG_fluxing_seqs as qwfs
from pycqed.measurement.waveform_control_CC.instruction_lib import convert_to_clocks

from pycqed.measurement import detector_functions as det
import pycqed.measurement.gate_set_tomography.gate_set_tomography_CC as gstCC


class CCLight_Transmon(Qubit):

    '''
    The CCLight_Transmon
    Setup configuration:
        Drive:                 CCLight controlling AWG8's and a VSM
        Acquisition:           UHFQC
        Readout pulse configuration: LO modulated using UHFQC AWG
    '''

    def __init__(self, name, **kw):
        t0 = time.time()
        super().__init__(name, **kw)
        self.add_parameters()
        self.connect_message(begin_time=t0)

    def add_instrument_ref_parameters(self):
        # MW sources
        self.add_parameter('instr_LO', parameter_class=InstrumentRefParameter)
        self.add_parameter('instr_cw_source',
                           parameter_class=InstrumentRefParameter)
        self.add_parameter('instr_td_source',
                           parameter_class=InstrumentRefParameter)

        # Control electronics
        self.add_parameter(
            'instr_CC', label='Central Controller',
            docstring=('Device responsible for controlling the experiment'
                       ' using eQASM generated using OpenQL, in the near'
                       ' future will be the CC_Light.'),
            parameter_class=InstrumentRefParameter)
        self.add_parameter('instr_acquisition',
                           parameter_class=InstrumentRefParameter)
        self.add_parameter('instr_VSM', label='Vector Switch Matrix',
                           parameter_class=InstrumentRefParameter)

        self.add_parameter('instr_MC', label='MeasurementControl',
                           parameter_class=InstrumentRefParameter)
        self.add_parameter('instr_SH', label='SignalHound',
                           parameter_class=InstrumentRefParameter)

        # LutMan's
        self.add_parameter('instr_LutMan_MW',
                           docstring='Lookuptable manager  for '
                           'microwave control pulses.',
                           parameter_class=InstrumentRefParameter)
        self.add_parameter('instr_LutMan_RO',
                           docstring='Lookuptable manager responsible for '
                           'microwave readout pulses.',
                           parameter_class=InstrumentRefParameter)
        self.add_parameter('instr_LutMan_Flux',
                           docstring='Lookuptable manager responsible for '
                                     'flux pulses.',
                           initial_value=None,
                           parameter_class=InstrumentRefParameter)

    def add_ro_parameters(self):
        """
        Adding the parameters relevant for readout.
        """
        ##########################
        # RO stimulus parameters #
        ##########################
        self.add_parameter('ro_freq',
                           label='Readout frequency', unit='Hz',
                           parameter_class=ManualParameter)
        self.add_parameter('ro_freq_mod',
                           label='Readout-modulation frequency', unit='Hz',
                           initial_value=-2e6,
                           parameter_class=ManualParameter)
        self.add_parameter('ro_pow_LO', label='RO power LO',
                           unit='dBm', initial_value=14,
                           parameter_class=ManualParameter)

        # RO pulse parameters

        self.add_parameter('ro_pulse_type', initial_value='IQmod_UHFQC',
                           vals=vals.Enum(
                               # 'Gated_CBox', 'Gated_UHFQC',
                               # 'IQmod_CBox',
                               # These other types are currently not supported
                               'IQmod_UHFQC', 'IQmod_multiplexed_UHFQC'),
                           parameter_class=ManualParameter)


        # self.add_parameter('ro_power_cw', label='RO power cw',
        #                    unit='dBm',
        #                    parameter_class=ManualParameter)

        # Mixer offsets correction, RO pulse
        self.add_parameter('ro_mixer_offs_I', unit='V',
                           parameter_class=ManualParameter, initial_value=0)
        self.add_parameter('ro_mixer_offs_Q', unit='V',
                           parameter_class=ManualParameter, initial_value=0)

        #############################
        # RO acquisition parameters #
        #############################

        ro_acq_docstr = (
            'Determines what type of integration weights to use: '
            '\n\t SSB: Single sideband demodulation\n\t'
            'DSB: Double sideband demodulation\n\t'
            'optimal: waveforms specified in "RO_acq_weight_func_I" '
            '\n\tand "RO_acq_weight_func_Q"')

        self.add_parameter('ro_acq_weight_type',
                           initial_value='DSB',
                           vals=vals.Enum('SSB', 'DSB', 'optimal'),
                           docstring=ro_acq_docstr,
                           parameter_class=ManualParameter)

        self.add_parameter(
            'ro_acq_weight_chI', initial_value=0, docstring=(
                'Determines the I-channel for integration. When the'
                ' ro_acq_weight_type is optimal only this channel will '
                'affect the result.'), vals=vals.Ints(0, 5),
            parameter_class=ManualParameter)
        self.add_parameter(
            'ro_acq_weight_chQ', initial_value=1, docstring=(
                'Determines the Q-channel for integration.'),
            vals=vals.Ints(0, 5), parameter_class=ManualParameter)

        self.add_parameter('ro_acq_weight_func_I',
                           vals=vals.Arrays(),
                           label='Optimized weights for I channel',
                           parameter_class=ManualParameter)
        self.add_parameter('ro_acq_weight_func_Q',
                           vals=vals.Arrays(),
                           label='Optimized weights for Q channel',
                           parameter_class=ManualParameter)






        self.add_parameter('ro_acq_integration_length', initial_value=500e-9,
                           vals=vals.Numbers(min_value=0, max_value=20e6),
                           parameter_class=ManualParameter)

        self.add_parameter('ro_acq_averages', initial_value=1024,
                           vals=vals.Numbers(min_value=0, max_value=1e6),
                           parameter_class=ManualParameter)

        self.add_parameter('ro_soft_averages', initial_value=1,
                           vals=vals.Ints(min_value=1),
                           parameter_class=ManualParameter)



        # Single shot readout specific parameters
        self.add_parameter('ro_digitized', vals=vals.Bool(),
                           initial_value=False,
                           parameter_class=ManualParameter)
        self.add_parameter('ro_threshold', unit='dac-value',
                           initial_value=0,
                           parameter_class=ManualParameter)
        self.add_parameter('ro_rotation_angle', unit='deg',
                           initial_value=0,
                           vals=vals.Numbers(0, 360),
                           parameter_class=ManualParameter)



        self.add_parameter('ro_depletion_time', initial_value=1e-6,
                           unit='s',
                           parameter_class=ManualParameter,
                           vals=vals.Numbers(min_value=0))

        self.add_parameter('ro_acq_period_cw', unit='s',
                           parameter_class=ManualParameter,
                           vals=vals.Numbers(0, 500e-6),
                           initial_value=10e-6)

        self.add_parameter('cal_pt_zero',
                           initial_value=None,
                           vals=vals.Anything(),  # should be a tuple validator
                           label='Calibration point |0>',
                           parameter_class=ManualParameter)
        self.add_parameter('cal_pt_one',
                           initial_value=None,
                           vals=vals.Anything(),  # should be a tuple validator
                           label='Calibration point |1>',
                           parameter_class=ManualParameter)

    def add_mw_parameters(self):
        self.add_parameter('mod_amp_td', label='RO modulation ampl td',
                           unit='V', initial_value=0.5,
                           parameter_class=ManualParameter)

        # Mixer skewness correction
        self.add_parameter('mixer_drive_phi', unit='deg',
                           parameter_class=ManualParameter, initial_value=0)
        self.add_parameter('mixer_drive_alpha', unit='',
                           parameter_class=ManualParameter, initial_value=1)
        # Mixer offsets correction, qubit drive
        self.add_parameter('mixer_offs_drive_I',
                           unit='V',
                           parameter_class=ManualParameter, initial_value=0)
        self.add_parameter('mixer_offs_drive_Q', unit='V',
                           parameter_class=ManualParameter, initial_value=0)

        self.add_parameter('td_source_pow',
                           label='Time-domain power',
                           unit='dBm',
                           parameter_class=ManualParameter)

        self.add_parameter('f_pulse_mod',
                           initial_value=-2e6,
                           label='pulse-modulation frequency', unit='Hz',
                           parameter_class=ManualParameter)
        self.add_parameter('Q_awg_nr', label='CBox awg nr', unit='#',
                           vals=vals.Ints(),
                           initial_value=0,
                           parameter_class=ManualParameter)

        self.add_parameter('Q_amp180',
                           label='Pi-pulse amplitude', unit='V',
                           initial_value=0.3,
                           parameter_class=ManualParameter)
        self.add_parameter('Q_amp90_scale',
                           label='pulse amplitude scaling factor',
                           unit='',
                           initial_value=.5,
                           vals=vals.Numbers(min_value=0, max_value=1.0),
                           parameter_class=ManualParameter)

        self.add_parameter('gauss_width', unit='s',
                           initial_value=10e-9,
                           parameter_class=ManualParameter)
        self.add_parameter('motzoi', label='Motzoi parameter', unit='',
                           initial_value=0,
                           parameter_class=ManualParameter)

    def add_spec_parameters(self):

        self.add_parameter('spec_pow', label='spectroscopy power',
                           unit='dBm',
                           parameter_class=ManualParameter)
        self.add_parameter('spec_pow_pulsed',
                           label='pulsed spectroscopy power',
                           unit='dBm',
                           parameter_class=ManualParameter)

        self.add_parameter('mod_amp_cw', label='RO modulation ampl cw',
                           unit='V', initial_value=0.5,
                           parameter_class=ManualParameter)

        self.add_parameter('spec_pulse_marker_channel',
                           vals=vals.Ints(1, 7),
                           initial_value=5,
                           parameter_class=ManualParameter)
        self.add_parameter('spec_pulse_type',
                           vals=vals.Enum('gated', 'square'),
                           initial_value='gated',
                           docstring=('Use either a marker gated spec pulse or' +
                                      ' use an AWG pulse to modulate a pulse'),
                           parameter_class=ManualParameter)

        self.add_parameter('spec_pulse_length',
                           label='Pulsed spec pulse duration',
                           unit='s',
                           vals=vals.Numbers(5e-9, 20e-6),
                           initial_value=500e-9,
                           parameter_class=ManualParameter)
        self.add_parameter('spec_amp',
                           unit='V',
                           vals=vals.Numbers(0, 1),
                           parameter_class=ManualParameter,
                           initial_value=0.4)

    def add_flux_parameters(self):
        pass

    def add_config_parameters(self):
        self.add_parameter(
            'cfg_trigger_period', label='Trigger period',
            docstring=('Time between experiments, used to initialize all'
                       ' qubits in the ground state'),
            unit='s', initial_value=200e-6,
            parameter_class=ManualParameter,
            vals=vals.Numbers(min_value=1e-6, max_value=327668e-9))

    def add_generic_qubit_parameters(self):
        pass

    def prepare_for_continuous_wave(self):
        self.prepare_readout()

        # LO and RF for readout are turned on in prepare_readout
        self.instr_td_source.get_instr().off()
        self.instr_cw_source.get_instr().off()
        self.instr_cw_source.get_instr().pulsemod_state.set('off')
        self.instr_cw_source.get_instr().power.set(self.spec_pow.get())

    def prepare_readout(self):
        """
        Configures the readout. Consists of the following steps
        - instantiate the relevant detector functions
        - set the microwave frequencies and sources
        - generate the RO pulse
        - set the integration weights
        """
        self._prep_ro_instantiate_detectors()
        self._prep_ro_sources()
        # self._generate_ro_pulse()
        self._prep_ro_integration_weights()

    def _prep_ro_instantiate_detectors(self):
        if self.ro_acq_weight_type() == 'optimal':
            ro_channels = [self.ro_acq_weight_chI()]
            result_logging_mode = 'lin_trans'

            if self.ro_digitized():
                result_logging_mode = 'digitized'
            # Update the RO theshold
            acq_ch = self.ro_acq_weight_chI()

            # The threshold that is set in the hardware  needs to be
            # corrected for the offset as this is only applied in
            # software.
            threshold = self.ro_threshold()
            offs = self.instr_acquisition.get_instr().get(
                'quex_trans_offset_weightfunction_{}'.format(acq_ch))
            hw_threshold = threshold + offs
            self.instr_acquisition.get_instr().set(
                'quex_thres_{}_level'.format(acq_ch), hw_threshold)

        else:
            ro_channels = [self.ro_acq_weight_chI(),
                           self.ro_acq_weight_chQ()]
            result_logging_mode = 'raw'

        if 'UHFQC' in self.instr_acquisition():
            UHFQC = self.instr_acquisition.get_instr()

            self.input_average_detector = det.UHFQC_input_average_detector(
                UHFQC=UHFQC,
                AWG=self.instr_CC.get_instr(),
                nr_averages=self.ro_acq_averages())

            self.int_avg_det = det.UHFQC_integrated_average_detector(
                UHFQC=UHFQC, AWG=self.instr_CC.get_instr(),
                channels=ro_channels,
                result_logging_mode=result_logging_mode,
                nr_averages=self.ro_acq_averages(),
                integration_length=self.ro_acq_integration_length())

            self.int_avg_det_single = det.UHFQC_integrated_average_detector(
                UHFQC=UHFQC, AWG=self.instr_CC.get_instr(),
                channels=ro_channels,
                result_logging_mode=result_logging_mode,
                nr_averages=self.ro_acq_averages(),
                real_imag=True, single_int_avg=True,
                integration_length=self.ro_acq_integration_length())

            self.int_log_det = det.UHFQC_integration_logging_det(
                UHFQC=UHFQC, AWG=self.instr_CC.get_instr(),
                channels=ro_channels,
                result_logging_mode=result_logging_mode,
                integration_length=self.ro_acq_integration_length())

    def _prep_ro_sources(self):
        LO = self.instr_LO.get_instr()
        LO.frequency.set(self.ro_freq() - self.ro_freq_mod())
        LO.on()
        LO.power(self.ro_pow_LO())

        if "gated" in self.ro_pulse_type().lower():
            raise NotImplementedError()
            # RF = self.ro_freq_source.get_instr()
            # RF.power(self.ro_power_cw())
            # RF.frequency(self.ro_freq.get())
            # RF.on()

    def _generate_ro_pulse(self):
        if 'CBox' in self.instr_acquisition():
            if 'multiplexed' not in self.ro_pulse_type().lower():
                self.ro_LutMan.get_instr().M_modulation(self.ro_freq_mod())
                self.ro_LutMan.get_instr().M_amp(self.ro_amp())
                self.ro_LutMan.get_instr().M_length(self.ro_pulse_length())

                if 'awg_nr' in self.ro_LutMan.get_instr().parameters:
                    self.ro_LutMan.get_instr().awg_nr(self.ro_awg_nr())

                if 'CBox' in self.instr_acquisition():
                    self.CBox.get_instr().set('AWG{:.0g}_dac0_offset'.format(
                                              self.ro_awg_nr.get()),
                                              self.mixer_offs_ro_I.get())
                    self.CBox.get_instr().set('AWG{:.0g}_dac1_offset'.format(
                                              self.ro_awg_nr.get()),
                                              self.mixer_offs_ro_Q.get())
                    if self.ro_LutMan() is not None:
                        self.ro_LutMan.get_instr().lut_mapping(
                            ['I', 'X180', 'Y180', 'X90', 'Y90', 'mX90', 'mY90', 'M_square'])

                    self.CBox.get_instr().integration_length(
                        convert_to_clocks(self.ro_acq_integration_length()))

                    self.CBox.get_instr().set('sig{}_threshold_line'.format(
                        int(self.signal_line.get())),
                        int(self.ro_threshold.get()))
                    self.CBox.get_instr().lin_trans_coeffs(
                        np.reshape(rotation_matrix(self.ro_rotation_angle(),
                                                   as_array=True), (4,)))

                    self.CBox.get_instr().set('sig{}_threshold_line'.format(
                        int(self.signal_line.get())),
                        int(self.ro_threshold.get()))
                self.ro_LutMan.get_instr().load_pulses_onto_AWG_lookuptable()

        elif 'UHFQC' in self.instr_acquisition():
            if 'gated' in self.ro_pulse_type().lower():
                UHFQC = self.instr_acquisition.get_instr()
                UHFQC.awg_sequence_acquisition()
            elif 'iqmod' in self.ro_pulse_type().lower():
                ro_lm = self.ro_LutMan.get_instr()
                ro_lm.M_length(self.ro_pulse_length())
                ro_lm.M_amp(self.ro_amp())
                ro_lm.M_length(self.ro_pulse_length())
                ro_lm.M_modulation(self.ro_freq_mod())
                ro_lm.acquisition_delay(self.ro_acq_marker_delay())

                if 'multiplexed' not in self.ro_pulse_type().lower():
                    ro_lm.load_pulse_onto_AWG_lookuptable('M_square')

    def _prep_ro_integration_weights(self):
        """
        Sets the ro acquisition integration weights.
        The relevant parameters here are
            ro_acq_weight_type   -> 'SSB', 'DSB' or 'Optimal'
            ro_acq_weight_chI    -> Specifies which integration weight (channel)
            ro_acq_weight_chQ    -> The second channel in case of SSB/DSB
            RO_acq_weight_func_I -> A custom integration weight (array)
            RO_acq_weight_func_Q ->  ""

        """
        if 'UHFQC' in self.instr_acquisition():
            UHFQC = self.instr_acquisition.get_instr()
            if self.ro_acq_weight_type() == 'SSB':
                UHFQC.prepare_SSB_weight_and_rotation(
                    IF=self.ro_freq_mod(),
                    weight_function_I=self.ro_acq_weight_chI(),
                    weight_function_Q=self.ro_acq_weight_chQ())
            elif self.ro_acq_weight_type() == 'DSB':
                UHFQC.prepare_DSB_weight_and_rotation(
                    IF=self.ro_freq_mod(),
                    weight_function_I=self.ro_acq_weight_chI(),
                    weight_function_Q=self.ro_acq_weight_chQ())
            elif self.ro_acq_weight_type() == 'optimal':
                if (self.ro_acq_weight_func_I() is None or
                        self.ro_acq_weight_func_Q() is None):
                    logging.warning('Optimal weights are None,' +
                                    ' not setting integration weights')
                else:
                    # When optimal weights are used, only the RO I weight
                    # channel is used
                    UHFQC.set('quex_wint_weights_{}_real'.format(
                        self.ro_acq_weight_chI()),
                        self.ro_acq_weight_func_I())
                    UHFQC.set('quex_wint_weights_{}_imag'.format(
                        self.ro_acq_weight_chI()),
                        self.ro_acq_weight_func_Q())
                    UHFQC.set('quex_rot_{}_real'.format(
                        self.ro_acq_weight_chI()), 1.0)
                    UHFQC.set('quex_rot_{}_imag'.format(
                        self.ro_acq_weight_chI()), -1.0)
        else:
            raise NotImplementedError(
                'CBox, DDM or other are currently not supported')

    def prepare_for_timedomain(self):
        pass

    def prepare_for_fluxing(self, reset=True):
        pass

    def _get_acquisition_instr(self):
        pass

    def _set_acquisition_instr(self, acq_instr_name):
        pass
