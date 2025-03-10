# -*- coding: utf-8 -*-
# Authors: Alexandre Gramfort <alexandre.gramfort@inria.fr>
#          Matti Hämäläinen <msh@nmr.mgh.harvard.edu>
#          Teon Brooks <teon.brooks@gmail.com>
#          Stefan Appelhoff <stefan.appelhoff@mailbox.org>
#
# License: BSD (3-clause)

from collections import Counter
from copy import deepcopy
import datetime
from io import BytesIO
import operator

import numpy as np
from scipy import linalg

from .pick import channel_type, pick_channels, pick_info
from .constants import FIFF
from .open import fiff_open
from .tree import dir_tree_find
from .tag import read_tag, find_tag, _coord_dict
from .proj import _read_proj, _write_proj, _uniquify_projs, _normalize_proj
from .ctf_comp import read_ctf_comp, write_ctf_comp
from .write import (start_file, end_file, start_block, end_block,
                    write_string, write_dig_points, write_float, write_int,
                    write_coord_trans, write_ch_info, write_name_list,
                    write_julian, write_float_matrix, write_id, DATE_NONE)
from .proc_history import _read_proc_history, _write_proc_history
from ..transforms import invert_transform, Transform
from ..utils import (logger, verbose, warn, object_diff, _validate_type,
                     _stamp_to_dt, _dt_to_stamp)
from ._digitization import (_format_dig_points, _dig_kind_proper,
                            _dig_kind_rev, _dig_kind_ints, _read_dig_fif)
from ._digitization import write_dig as _dig_write_dig
from .compensator import get_current_comp

b = bytes  # alias

_kind_dict = dict(
    eeg=(FIFF.FIFFV_EEG_CH, FIFF.FIFFV_COIL_EEG, FIFF.FIFF_UNIT_V),
    mag=(FIFF.FIFFV_MEG_CH, FIFF.FIFFV_COIL_VV_MAG_T3, FIFF.FIFF_UNIT_T),
    grad=(FIFF.FIFFV_MEG_CH, FIFF.FIFFV_COIL_VV_PLANAR_T1, FIFF.FIFF_UNIT_T_M),
    ref_meg=(FIFF.FIFFV_REF_MEG_CH, FIFF.FIFFV_COIL_VV_MAG_T3,
             FIFF.FIFF_UNIT_T),
    misc=(FIFF.FIFFV_MISC_CH, FIFF.FIFFV_COIL_NONE, FIFF.FIFF_UNIT_NONE),
    stim=(FIFF.FIFFV_STIM_CH, FIFF.FIFFV_COIL_NONE, FIFF.FIFF_UNIT_V),
    eog=(FIFF.FIFFV_EOG_CH, FIFF.FIFFV_COIL_NONE, FIFF.FIFF_UNIT_V),
    ecg=(FIFF.FIFFV_ECG_CH, FIFF.FIFFV_COIL_NONE, FIFF.FIFF_UNIT_V),
    emg=(FIFF.FIFFV_EMG_CH, FIFF.FIFFV_COIL_NONE, FIFF.FIFF_UNIT_V),
    seeg=(FIFF.FIFFV_SEEG_CH, FIFF.FIFFV_COIL_EEG, FIFF.FIFF_UNIT_V),
    bio=(FIFF.FIFFV_BIO_CH, FIFF.FIFFV_COIL_NONE, FIFF.FIFF_UNIT_V),
    ecog=(FIFF.FIFFV_ECOG_CH, FIFF.FIFFV_COIL_EEG, FIFF.FIFF_UNIT_V),
    fnirs_raw=(FIFF.FIFFV_FNIRS_CH, FIFF.FIFFV_COIL_FNIRS_RAW,
               FIFF.FIFF_UNIT_V),
    fnirs_od=(FIFF.FIFFV_FNIRS_CH, FIFF.FIFFV_COIL_FNIRS_OD,
              FIFF.FIFF_UNIT_NONE),
    hbo=(FIFF.FIFFV_FNIRS_CH, FIFF.FIFFV_COIL_FNIRS_HBO, FIFF.FIFF_UNIT_MOL),
    hbr=(FIFF.FIFFV_FNIRS_CH, FIFF.FIFFV_COIL_FNIRS_HBR, FIFF.FIFF_UNIT_MOL),
    csd=(FIFF.FIFFV_EEG_CH, FIFF.FIFFV_COIL_EEG_CSD, FIFF.FIFF_UNIT_V_M2),
)


def _get_valid_units():
    """Get valid units according to the International System of Units (SI).

    The International System of Units (SI, [1]_) is the default system for
    describing units in the Brain Imaging Data Structure (BIDS). For more
    information, see the BIDS specification [2]_ and the appendix "Units"
    therein.

    References
    ----------
    [1] .. https://en.wikipedia.org/wiki/International_System_of_Units
    [2] .. https://bids-specification.readthedocs.io/en/stable/

    """
    valid_prefix_names = ['yocto', 'zepto', 'atto', 'femto', 'pico', 'nano',
                          'micro', 'milli', 'centi', 'deci', 'deca', 'hecto',
                          'kilo', 'mega', 'giga', 'tera', 'peta', 'exa',
                          'zetta', 'yotta']
    valid_prefix_symbols = ['y', 'z', 'a', 'f', 'p', 'n', u'µ', 'm', 'c', 'd',
                            'da', 'h', 'k', 'M', 'G', 'T', 'P', 'E', 'Z', 'Y']
    valid_unit_names = ['metre', 'kilogram', 'second', 'ampere', 'kelvin',
                        'mole', 'candela', 'radian', 'steradian', 'hertz',
                        'newton', 'pascal', 'joule', 'watt', 'coulomb', 'volt',
                        'farad', 'ohm', 'siemens', 'weber', 'tesla', 'henry',
                        'degree Celsius', 'lumen', 'lux', 'becquerel', 'gray',
                        'sievert', 'katal']
    valid_unit_symbols = ['m', 'kg', 's', 'A', 'K', 'mol', 'cd', 'rad', 'sr',
                          'Hz', 'N', 'Pa', 'J', 'W', 'C', 'V', 'F', u'Ω', 'S',
                          'Wb', 'T', 'H', u'°C', 'lm', 'lx', 'Bq', 'Gy', 'Sv',
                          'kat']

    # Valid units are all possible combinations of either prefix name or prefix
    # symbol together with either unit name or unit symbol. E.g., nV for
    # nanovolt
    valid_units = []
    valid_units += ([''.join([prefix, unit]) for prefix in valid_prefix_names
                     for unit in valid_unit_names])
    valid_units += ([''.join([prefix, unit]) for prefix in valid_prefix_names
                     for unit in valid_unit_symbols])
    valid_units += ([''.join([prefix, unit]) for prefix in valid_prefix_symbols
                     for unit in valid_unit_names])
    valid_units += ([''.join([prefix, unit]) for prefix in valid_prefix_symbols
                     for unit in valid_unit_symbols])

    # units are also valid without a prefix
    valid_units += valid_unit_names
    valid_units += valid_unit_symbols

    # we also accept "n/a" as a unit, which is the default missing value in
    # BIDS
    valid_units += ["n/a"]

    return tuple(valid_units)


def _summarize_str(st):
    """Make summary string."""
    return st[:56][::-1].split(',', 1)[-1][::-1] + ', ...'


def _unique_channel_names(ch_names):
    """Ensure unique channel names."""
    FIFF_CH_NAME_MAX_LENGTH = 15
    unique_ids = np.unique(ch_names, return_index=True)[1]
    if len(unique_ids) != len(ch_names):
        dups = {ch_names[x]
                for x in np.setdiff1d(range(len(ch_names)), unique_ids)}
        warn('Channel names are not unique, found duplicates for: '
             '%s. Applying running numbers for duplicates.' % dups)
        for ch_stem in dups:
            overlaps = np.where(np.array(ch_names) == ch_stem)[0]
            # We need an extra character since we append '-'.
            # np.ceil(...) is the maximum number of appended digits.
            n_keep = (FIFF_CH_NAME_MAX_LENGTH - 1 -
                      int(np.ceil(np.log10(len(overlaps)))))
            n_keep = min(len(ch_stem), n_keep)
            ch_stem = ch_stem[:n_keep]
            for idx, ch_idx in enumerate(overlaps):
                ch_name = ch_stem + '-%s' % idx
                if ch_name not in ch_names:
                    ch_names[ch_idx] = ch_name
                else:
                    raise ValueError('Adding a running number for a '
                                     'duplicate resulted in another '
                                     'duplicate name %s' % ch_name)

    return ch_names


# XXX Eventually this should be de-duplicated with the MNE-MATLAB stuff...
class Info(dict):
    """Measurement information.

    This data structure behaves like a dictionary. It contains all metadata
    that is available for a recording.

    This class should not be instantiated directly. To create a measurement
    information strucure, use :func:`mne.create_info`.

    The only entries that should be manually changed by the user are
    ``info['bads']`` and ``info['description']``. All other entries should
    be considered read-only, or should be modified by functions or methods.

    Attributes
    ----------
    acq_pars : str | None
        MEG system acquition parameters.
        See :class:`mne.AcqParserFIF` for details.
    acq_stim : str | None
        MEG system stimulus parameters.
    bads : list of str
        List of bad (noisy/broken) channels, by name. These channels will by
        default be ignored by many processing steps.
    ch_names : list of str
        The names of the channels.
    chs : list of dict
        A list of channel information dictionaries, one per channel.
        See Notes for more information.
    comps : list of dict
        CTF software gradient compensation data.
        See Notes for more information.
    ctf_head_t : dict | None
        The transformation from 4D/CTF head coordinates to Neuromag head
        coordinates. This is only present in 4D/CTF data.
    custom_ref_applied : int
        Whether a custom (=other than average) reference has been applied to
        the EEG data. This flag is checked by some algorithms that require an
        average reference to be set.
    description : str | None
        String description of the recording.
    dev_ctf_t : dict | None
        The transformation from device coordinates to 4D/CTF head coordinates.
        This is only present in 4D/CTF data.
    dev_head_t : dict | None
        The device to head transformation.
    dig : list of dict | None
        The Polhemus digitization data in head coordinates.
        See Notes for more information.
    events : list of dict
        Event list, sometimes extracted from the stim channels by Neuromag
        systems. In general this should not be used and
        :func:`mne.find_events` should be used for event processing.
        See Notes for more information.
    experimenter : str | None
        Name of the person that ran the experiment.
    file_id : dict | None
        The FIF globally unique ID. See Notes for more information.
    highpass : float
        Highpass corner frequency in Hertz. Zero indicates a DC recording.
    hpi_meas : list of dict
        HPI measurements that were taken at the start of the recording
        (e.g. coil frequencies).
        See Notes for details.
    hpi_results : list of dict
        Head position indicator (HPI) digitization points and fit information
        (e.g., the resulting transform).
        See Notes for details.
    hpi_subsystem : dict | None
        Information about the HPI subsystem that was used (e.g., event
        channel used for cHPI measurements).
        See Notes for details.
    line_freq : float | None
        Frequency of the power line in Hertz.
    gantry_angle : float | None
        Tilt angle of the gantry in degrees.
    lowpass : float
        Lowpass corner frequency in Hertz.
    meas_date : datetime
        The time (UTC) of the recording.

        .. versionchanged:: 0.20
           This is stored as a :class:`~python:datetime.datetime` object
           instead of a tuple of seconds/microseconds.
    utc_offset : str
        "UTC offset of related meas_date (sHH:MM).

        .. versionadded:: 0.19
    meas_id : dict | None
        The ID assigned to this measurement by the acquisition system or
        during file conversion. Follows the same format as ``file_id``.
    nchan : int
        Number of channels.
    proc_history : list of dict
        The MaxFilter processing history.
        See Notes for details.
    proj_id : int | None
        ID number of the project the experiment belongs to.
    proj_name : str | None
        Name of the project the experiment belongs to.
    projs : list of Projection
        List of SSP operators that operate on the data.
        See :class:`mne.Projection` for details.
    sfreq : float
        Sampling frequency in Hertz.
    subject_info : dict | None
        Information about the subject.
        See Notes for details.
    device_info : dict | None
        Information about the acquisition device. See Notes for details.

        .. versionadded:: 0.19
    helium_info : dict | None
        Information about the device helium. See Notes for details.

        .. versionadded:: 0.19

    See Also
    --------
    mne.create_info

    Notes
    -----
    The following parameters have a nested structure.

    * ``chs`` list of dict:

        cal : float
            The calibration factor to bring the channels to physical
            units. Used in product with ``range`` to scale the data read
            from disk.
        ch_name : str
            The channel name.
        coil_type : int
            Coil type, e.g. ``FIFFV_COIL_MEG``.
        coord_frame : int
            The coordinate frame used, e.g. ``FIFFV_COORD_HEAD``.
        kind : int
            The kind of channel, e.g. ``FIFFV_EEG_CH``.
        loc : array, shape (12,)
            Channel location. For MEG this is the position plus the
            normal given by a 3x3 rotation matrix. For EEG this is the
            position followed by reference position (with 6 unused).
            The values are specified in device coordinates for MEG and in
            head coordinates for EEG channels, respectively.
        logno : int
            Logical channel number, conventions in the usage of this
            number vary.
        range : float
            The hardware-oriented part of the calibration factor.
            This should be only applied to the continuous raw data.
            Used in product with ``cal`` to scale data read from disk.
        scanno : int
            Scanning order number, starting from 1.
        unit : int
            The unit to use, e.g. ``FIFF_UNIT_T_M``.
        unit_mul : int
            Unit multipliers, most commonly ``FIFF_UNITM_NONE``.

    * ``comps`` list of dict:

        ctfkind : int
            CTF compensation grade.
        colcals : ndarray
            Column calibrations.
        mat : dict
            A named matrix dictionary (with entries "data", "col_names", etc.)
            containing the compensation matrix.
        rowcals : ndarray
            Row calibrations.
        save_calibrated : bool
            Were the compensation data saved in calibrated form.

    * ``dig`` list of dict:

        kind : int
            The kind of channel,
            e.g. ``FIFFV_POINT_EEG``, ``FIFFV_POINT_CARDINAL``.
        r : array, shape (3,)
            3D position in m. and coord_frame.
        ident : int
            Number specifying the identity of the point.
            e.g. ``FIFFV_POINT_NASION`` if kind is ``FIFFV_POINT_CARDINAL``, or
            42 if kind is ``FIFFV_POINT_EEG``.
        coord_frame : int
            The coordinate frame used, e.g. ``FIFFV_COORD_HEAD``.

    * ``events`` list of dict:

        channels : list of int
            Channel indices for the events.
        list : ndarray, shape (n_events * 3,)
            Events in triplets as number of samples, before, after.

    * ``file_id`` dict:

        version : int
            FIF format version, i.e. ``FIFFC_VERSION``.
        machid : ndarray, shape (2,)
            Unique machine ID, usually derived from the MAC address.
        secs : int
            Time in seconds.
        usecs : int
            Time in microseconds.

    * ``hpi_meas`` list of dict:

        creator : str
            Program that did the measurement.
        sfreq : float
            Sample rate.
        nchan : int
            Number of channels used.
        nave : int
            Number of averages used.
        ncoil : int
            Number of coils used.
        first_samp : int
            First sample used.
        last_samp : int
            Last sample used.
        hpi_coils : list of dict
            Coils, containing:

                number: int
                    Coil number
                epoch : ndarray
                    Buffer containing one epoch and channel.
                slopes : ndarray, shape (n_channels,)
                    HPI data.
                corr_coeff : ndarray, shape (n_channels,)
                    HPI curve fit correlations.
                coil_freq : float
                    HPI coil excitation frequency

    * ``hpi_results`` list of dict:

        dig_points : list
            Digitization points (see ``dig`` definition) for the HPI coils.
        order : ndarray, shape (ncoil,)
            The determined digitization order.
        used : ndarray, shape (nused,)
            The indices of the used coils.
        moments : ndarray, shape (ncoil, 3)
            The coil moments.
        goodness : ndarray, shape (ncoil,)
            The goodness of fits.
        good_limit : float
            The goodness of fit limit.
        dist_limit : float
            The distance limit.
        accept : int
            Whether or not the fit was accepted.
        coord_trans : instance of Transformation
            The resulting MEG<->head transformation.

    * ``hpi_subsystem`` dict:

        ncoil : int
            The number of coils.
        event_channel : str
            The event channel used to encode cHPI status (e.g., STI201).
        hpi_coils : list of ndarray
            List of length ``ncoil``, each 4-element ndarray contains the
            event bits used on the event channel to indicate cHPI status
            (using the first element of these arrays is typically
            sufficient).

    * ``proc_history`` list of dict:

        block_id : dict
            See ``id`` above.
        date : ndarray, shape (2,)
            2-element tuple of seconds and microseconds.
        experimenter : str
            Name of the person who ran the program.
        creator : str
            Program that did the processing.
        max_info : dict
            Maxwel filtering info, can contain:

                sss_info : dict
                    SSS processing information.
                max_st
                    tSSS processing information.
                sss_ctc : dict
                    Cross-talk processing information.
                sss_cal : dict
                    Fine-calibration information.
        smartshield : dict
            MaxShield information. This dictionary is (always?) empty,
            but its presence implies that MaxShield was used during
            acquisiton.

    * ``subject_info`` dict:

        id : int
            Integer subject identifier.
        his_id : str
            String subject identifier.
        last_name : str
            Last name.
        first_name : str
            First name.
        middle_name : str
            Middle name.
        birthday : tuple of int
            Birthday in (year, month, day) format.
        sex : int
            Subject sex (0=unknown, 1=male, 2=female).
        hand : int
            Handedness (1=right, 2=left).

    * ``device_info`` dict:

        type : str
            Device type.
        model : str
            Device model.
        serial : str
            Device serial.
        site : str
            Device site.

    * ``helium_info`` dict:

        he_level_raw : float
            Helium level (%) before position correction.
        helium_level : float
            Helium level (%) after position correction.
        orig_file_guid : str
            Original file GUID.
        meas_date : tuple of int
            The helium level meas date.
    """

    def copy(self):
        """Copy the instance.

        Returns
        -------
        info : instance of Info
            The copied info.
        """
        return Info(deepcopy(self))

    def normalize_proj(self):
        """(Re-)Normalize projection vectors after subselection.

        Applying projection after sub-selecting a set of channels that
        were originally used to compute the original projection vectors
        can be dangerous (e.g., if few channels remain, most power was
        in channels that are no longer picked, etc.). By default, mne
        will emit a warning when this is done.

        This function will re-normalize projectors to use only the
        remaining channels, thus avoiding that warning. Only use this
        function if you're confident that the projection vectors still
        adequately capture the original signal of interest.
        """
        _normalize_proj(self)

    def __repr__(self):
        """Summarize info instead of printing all."""
        strs = ['<Info | %s non-empty fields']
        non_empty = 0
        for k, v in self.items():
            if k in ['bads', 'ch_names']:
                entr = (', '.join(b for ii, b in enumerate(v) if ii < 10)
                        if v else '0 items')
                if len(v) > 10:
                    # get rid of of half printed ch names
                    entr = _summarize_str(entr)
            elif k == 'projs' and v:
                entr = ', '.join(p['desc'] + ': o%s' %
                                 {0: 'ff', 1: 'n'}[p['active']] for p in v)
                if len(entr) >= 56:
                    entr = _summarize_str(entr)
            elif k == 'meas_date':
                if v is None:
                    entr = 'unspecified'
                else:
                    # first entry in meas_date is meaningful
                    entr = v.strftime('%Y-%m-%d %H:%M:%S') + ' GMT'
            elif k == 'kit_system_id' and v is not None:
                from .kit.constants import KIT_SYSNAMES
                entr = '%i (%s)' % (v, KIT_SYSNAMES.get(v, 'unknown'))
            elif k == 'dig' and v is not None:
                counts = Counter(d['kind'] for d in v)
                counts = ['%d %s' % (counts[ii],
                                     _dig_kind_proper[_dig_kind_rev[ii]])
                          for ii in _dig_kind_ints if ii in counts]
                counts = (' (%s)' % (', '.join(counts))) if len(counts) else ''
                entr = '%d items%s' % (len(v), counts)
            else:
                this_len = (len(v) if hasattr(v, '__len__') else
                            ('%s' % v if v is not None else None))
                entr = (('%d items' % this_len) if isinstance(this_len, int)
                        else ('%s' % this_len if this_len else ''))
            if entr:
                non_empty += 1
                entr = ' | ' + entr
            if k == 'chs':
                ch_types = [channel_type(self, idx) for idx in range(len(v))]
                ch_counts = Counter(ch_types)
                entr += " (%s)" % ', '.join("%s: %d" % (ch_type.upper(), count)
                                            for ch_type, count
                                            in ch_counts.items())
            strs.append('%s : %s%s' % (k, type(v).__name__, entr))
            if k in ['sfreq', 'lowpass', 'highpass']:
                strs[-1] += ' Hz'
        strs_non_empty = sorted(s for s in strs if '|' in s)
        strs_empty = sorted(s for s in strs if '|' not in s)
        st = '\n    '.join(strs_non_empty + strs_empty)
        st += '\n>'
        st %= non_empty
        return st

    def _check_consistency(self, prepend_error=''):
        """Do some self-consistency checks and datatype tweaks."""
        missing = [bad for bad in self['bads'] if bad not in self['ch_names']]
        if len(missing) > 0:
            msg = '%sbad channel(s) %s marked do not exist in info'
            raise RuntimeError(msg % (prepend_error, missing,))
        meas_date = self.get('meas_date')
        if meas_date is not None:
            if (not isinstance(self['meas_date'], datetime.datetime) or
                    self['meas_date'].tzinfo is None or
                    self['meas_date'].tzinfo is not datetime.timezone.utc):
                raise RuntimeError('%sinfo["meas_date"] must be a datetime '
                                   'object in UTC or None, got "%r"'
                                   % (prepend_error, repr(self['meas_date']),))
            meas_date_stamp = _dt_to_stamp(meas_date)
            if (meas_date_stamp[0] < np.iinfo('>i4').min or
                    meas_date_stamp[0] > np.iinfo('>i4').max):
                raise RuntimeError(
                    '%sinfo["meas_date"] seconds must be between "%r" '
                    'and "%r", got "%r"'
                    % (prepend_error, (np.iinfo('>i4').min, 0),
                       (np.iinfo('>i4').max, 0), meas_date_stamp[0],))

        for key in ('file_id', 'meas_id'):
            value = self.get(key)
            if value is not None:
                assert 'msecs' not in value
                for key_2 in ('secs', 'usecs'):
                    if (value[key_2] < np.iinfo('>i4').min or
                            value[key_2] > np.iinfo('>i4').max):
                        raise RuntimeError('%sinfo[%s][%s] must be between '
                                           '"%r" and "%r", got "%r"'
                                           % (prepend_error, key, key_2,
                                              np.iinfo('>i4').min,
                                              np.iinfo('>i4').max,
                                              value[key_2]),)

        chs = [ch['ch_name'] for ch in self['chs']]
        if len(self['ch_names']) != len(chs) or any(
                ch_1 != ch_2 for ch_1, ch_2 in zip(self['ch_names'], chs)) or \
                self['nchan'] != len(chs):
            raise RuntimeError('%sinfo channel name inconsistency detected, '
                               'please notify mne-python developers'
                               % (prepend_error,))

        # make sure we have the proper datatypes
        for key in ('sfreq', 'highpass', 'lowpass'):
            if self.get(key) is not None:
                self[key] = float(self[key])

        # make sure channel names are not too long
        self._check_ch_name_length()

        # make sure channel names are unique
        self['ch_names'] = _unique_channel_names(self['ch_names'])
        for idx, ch_name in enumerate(self['ch_names']):
            self['chs'][idx]['ch_name'] = ch_name

        if 'filename' in self:
            warn('the "filename" key is misleading '
                 'and info should not have it')

    def _check_ch_name_length(self):
        """Check that channel names are sufficiently short."""
        bad_names = list()
        for ch in self['chs']:
            if len(ch['ch_name']) > 15:
                bad_names.append(ch['ch_name'])
                ch['ch_name'] = ch['ch_name'][:15]
        if len(bad_names) > 0:
            warn('%d channel names are too long, have been truncated to 15 '
                 'characters:\n%s' % (len(bad_names), bad_names))
            self._update_redundant()

    def _update_redundant(self):
        """Update the redundant entries."""
        self['ch_names'] = [ch['ch_name'] for ch in self['chs']]
        self['nchan'] = len(self['chs'])

    def pick_channels(self, ch_names, ordered=False):
        """Pick channels from this Info object.

        Parameters
        ----------
        ch_names : list of str
            List of channels to keep. All other channels are dropped.
        ordered : bool
            If True (default False), ensure that the order of the channels
            matches the order of ``ch_names``.

        Returns
        -------
        info : instance of Info.
            The modified Info object.

        Notes
        -----
        Operates in-place.

        .. versionadded:: 0.20.0
        """
        sel = pick_channels(self.ch_names, ch_names, exclude=[],
                            ordered=ordered)
        return pick_info(self, sel, copy=False, verbose=False)

    @property
    def ch_names(self):
        return self['ch_names']


def _simplify_info(info):
    """Return a simplified info structure to speed up picking."""
    chs = [{key: ch[key]
            for key in ('ch_name', 'kind', 'unit', 'coil_type', 'loc')}
           for ch in info['chs']]
    sub_info = Info(chs=chs, bads=info['bads'], comps=info['comps'],
                    projs=info['projs'],
                    custom_ref_applied=info['custom_ref_applied'])
    sub_info._update_redundant()
    return sub_info


@verbose
def read_fiducials(fname, verbose=None):
    """Read fiducials from a fiff file.

    Parameters
    ----------
    fname : str
        The filename to read.
    %(verbose)s

    Returns
    -------
    pts : list of dicts
        List of digitizer points (each point in a dict).
    coord_frame : int
        The coordinate frame of the points (one of
        mne.io.constants.FIFF.FIFFV_COORD_...).
    """
    fid, tree, _ = fiff_open(fname)
    with fid:
        isotrak = dir_tree_find(tree, FIFF.FIFFB_ISOTRAK)
        isotrak = isotrak[0]
        pts = []
        coord_frame = FIFF.FIFFV_COORD_HEAD
        for k in range(isotrak['nent']):
            kind = isotrak['directory'][k].kind
            pos = isotrak['directory'][k].pos
            if kind == FIFF.FIFF_DIG_POINT:
                tag = read_tag(fid, pos)
                pts.append(tag.data)
            elif kind == FIFF.FIFF_MNE_COORD_FRAME:
                tag = read_tag(fid, pos)
                coord_frame = tag.data[0]

    # coord_frame is not stored in the tag
    for pt in pts:
        pt['coord_frame'] = coord_frame

    return pts, coord_frame


@verbose
def write_fiducials(fname, pts, coord_frame=FIFF.FIFFV_COORD_UNKNOWN,
                    verbose=None):
    """Write fiducials to a fiff file.

    Parameters
    ----------
    fname : str
        Destination file name.
    pts : iterator of dict
        Iterator through digitizer points. Each point is a dictionary with
        the keys 'kind', 'ident' and 'r'.
    coord_frame : int
        The coordinate frame of the points (one of
        mne.io.constants.FIFF.FIFFV_COORD_...).
    %(verbose)s
    """
    _dig_write_dig(fname, pts, coord_frame)


def write_dig(fname, pts, coord_frame=None):
    """Write digitization data to a FIF file.

    Parameters
    ----------
    fname : str
        Destination file name.
    pts : iterator of dict
        Iterator through digitizer points. Each point is a dictionary with
        the keys 'kind', 'ident' and 'r'.
    coord_frame : int | str | None
        If all the points have the same coordinate frame, specify the type
        here. Can be None (default) if the points could have varying
        coordinate frames.
    """
    return _dig_write_dig(fname, pts, coord_frame=None)


@verbose
def read_info(fname, verbose=None):
    """Read measurement info from a file.

    Parameters
    ----------
    fname : str
        File name.
    %(verbose)s

    Returns
    -------
    info : instance of Info
       Measurement information for the dataset.
    """
    f, tree, _ = fiff_open(fname)
    with f as fid:
        info = read_meas_info(fid, tree)[0]
    return info


def read_bad_channels(fid, node):
    """Read bad channels.

    Parameters
    ----------
    fid : file
        The file descriptor.

    node : dict
        The node of the FIF tree that contains info on the bad channels.

    Returns
    -------
    bads : list
        A list of bad channel's names.
    """
    nodes = dir_tree_find(node, FIFF.FIFFB_MNE_BAD_CHANNELS)

    bads = []
    if len(nodes) > 0:
        for node in nodes:
            tag = find_tag(fid, node, FIFF.FIFF_MNE_CH_NAME_LIST)
            if tag is not None and tag.data is not None:
                bads = tag.data.split(':')
    return bads


@verbose
def read_meas_info(fid, tree, clean_bads=False, verbose=None):
    """Read the measurement info.

    Parameters
    ----------
    fid : file
        Open file descriptor.
    tree : tree
        FIF tree structure.
    clean_bads : bool
        If True, clean info['bads'] before running consistency check.
        Should only be needed for old files where we did not check bads
        before saving.
    %(verbose)s

    Returns
    -------
    info : instance of Info
       Info on dataset.
    meas : dict
        Node in tree that contains the info.
    """
    #   Find the desired blocks
    meas = dir_tree_find(tree, FIFF.FIFFB_MEAS)
    if len(meas) == 0:
        raise ValueError('Could not find measurement data')
    if len(meas) > 1:
        raise ValueError('Cannot read more that 1 measurement data')
    meas = meas[0]

    meas_info = dir_tree_find(meas, FIFF.FIFFB_MEAS_INFO)
    if len(meas_info) == 0:
        raise ValueError('Could not find measurement info')
    if len(meas_info) > 1:
        raise ValueError('Cannot read more that 1 measurement info')
    meas_info = meas_info[0]

    #   Read measurement info
    dev_head_t = None
    ctf_head_t = None
    dev_ctf_t = None
    meas_date = None
    utc_offset = None
    highpass = None
    lowpass = None
    nchan = None
    sfreq = None
    chs = []
    experimenter = None
    description = None
    proj_id = None
    proj_name = None
    line_freq = None
    gantry_angle = None
    custom_ref_applied = FIFF.FIFFV_MNE_CUSTOM_REF_OFF
    xplotter_layout = None
    kit_system_id = None
    for k in range(meas_info['nent']):
        kind = meas_info['directory'][k].kind
        pos = meas_info['directory'][k].pos
        if kind == FIFF.FIFF_NCHAN:
            tag = read_tag(fid, pos)
            nchan = int(tag.data)
        elif kind == FIFF.FIFF_SFREQ:
            tag = read_tag(fid, pos)
            sfreq = float(tag.data)
        elif kind == FIFF.FIFF_CH_INFO:
            tag = read_tag(fid, pos)
            chs.append(tag.data)
        elif kind == FIFF.FIFF_LOWPASS:
            tag = read_tag(fid, pos)
            if not np.isnan(tag.data):
                lowpass = float(tag.data)
        elif kind == FIFF.FIFF_HIGHPASS:
            tag = read_tag(fid, pos)
            if not np.isnan(tag.data):
                highpass = float(tag.data)
        elif kind == FIFF.FIFF_MEAS_DATE:
            tag = read_tag(fid, pos)
            meas_date = tuple(tag.data)
            if len(meas_date) == 1:  # can happen from old C conversions
                meas_date = (meas_date[0], 0)
        elif kind == FIFF.FIFF_UTC_OFFSET:
            tag = read_tag(fid, pos)
            utc_offset = str(tag.data)
        elif kind == FIFF.FIFF_COORD_TRANS:
            tag = read_tag(fid, pos)
            cand = tag.data

            if cand['from'] == FIFF.FIFFV_COORD_DEVICE and \
                    cand['to'] == FIFF.FIFFV_COORD_HEAD:
                dev_head_t = cand
            elif cand['from'] == FIFF.FIFFV_COORD_HEAD and \
                    cand['to'] == FIFF.FIFFV_COORD_DEVICE:
                # this reversal can happen with BabyMEG data
                dev_head_t = invert_transform(cand)
            elif cand['from'] == FIFF.FIFFV_MNE_COORD_CTF_HEAD and \
                    cand['to'] == FIFF.FIFFV_COORD_HEAD:
                ctf_head_t = cand
            elif cand['from'] == FIFF.FIFFV_MNE_COORD_CTF_DEVICE and \
                    cand['to'] == FIFF.FIFFV_MNE_COORD_CTF_HEAD:
                dev_ctf_t = cand
        elif kind == FIFF.FIFF_EXPERIMENTER:
            tag = read_tag(fid, pos)
            experimenter = tag.data
        elif kind == FIFF.FIFF_DESCRIPTION:
            tag = read_tag(fid, pos)
            description = tag.data
        elif kind == FIFF.FIFF_PROJ_ID:
            tag = read_tag(fid, pos)
            proj_id = tag.data
        elif kind == FIFF.FIFF_PROJ_NAME:
            tag = read_tag(fid, pos)
            proj_name = tag.data
        elif kind == FIFF.FIFF_LINE_FREQ:
            tag = read_tag(fid, pos)
            line_freq = float(tag.data)
        elif kind == FIFF.FIFF_GANTRY_ANGLE:
            tag = read_tag(fid, pos)
            gantry_angle = float(tag.data)
        elif kind in [FIFF.FIFF_MNE_CUSTOM_REF, 236]:  # 236 used before v0.11
            tag = read_tag(fid, pos)
            custom_ref_applied = int(tag.data)
        elif kind == FIFF.FIFF_XPLOTTER_LAYOUT:
            tag = read_tag(fid, pos)
            xplotter_layout = str(tag.data)
        elif kind == FIFF.FIFF_MNE_KIT_SYSTEM_ID:
            tag = read_tag(fid, pos)
            kit_system_id = int(tag.data)

    # Check that we have everything we need
    if nchan is None:
        raise ValueError('Number of channels is not defined')

    if sfreq is None:
        raise ValueError('Sampling frequency is not defined')

    if len(chs) == 0:
        raise ValueError('Channel information not defined')

    if len(chs) != nchan:
        raise ValueError('Incorrect number of channel definitions found')

    if dev_head_t is None or ctf_head_t is None:
        hpi_result = dir_tree_find(meas_info, FIFF.FIFFB_HPI_RESULT)
        if len(hpi_result) == 1:
            hpi_result = hpi_result[0]
            for k in range(hpi_result['nent']):
                kind = hpi_result['directory'][k].kind
                pos = hpi_result['directory'][k].pos
                if kind == FIFF.FIFF_COORD_TRANS:
                    tag = read_tag(fid, pos)
                    cand = tag.data
                    if (cand['from'] == FIFF.FIFFV_COORD_DEVICE and
                            cand['to'] == FIFF.FIFFV_COORD_HEAD and
                            dev_head_t is None):
                        dev_head_t = cand
                    elif (cand['from'] == FIFF.FIFFV_MNE_COORD_CTF_HEAD and
                          cand['to'] == FIFF.FIFFV_COORD_HEAD and
                          ctf_head_t is None):
                        ctf_head_t = cand

    #   Locate the Polhemus data
    dig = _read_dig_fif(fid, meas_info)

    #   Locate the acquisition information
    acqpars = dir_tree_find(meas_info, FIFF.FIFFB_DACQ_PARS)
    acq_pars = None
    acq_stim = None
    if len(acqpars) == 1:
        acqpars = acqpars[0]
        for k in range(acqpars['nent']):
            kind = acqpars['directory'][k].kind
            pos = acqpars['directory'][k].pos
            if kind == FIFF.FIFF_DACQ_PARS:
                tag = read_tag(fid, pos)
                acq_pars = tag.data
            elif kind == FIFF.FIFF_DACQ_STIM:
                tag = read_tag(fid, pos)
                acq_stim = tag.data

    #   Load the SSP data
    projs = _read_proj(fid, meas_info)

    #   Load the CTF compensation data
    comps = read_ctf_comp(fid, meas_info, chs)

    #   Load the bad channel list
    bads = read_bad_channels(fid, meas_info)

    #
    #   Put the data together
    #
    if tree['id'] is not None:
        info = Info(file_id=tree['id'])
    else:
        info = Info(file_id=None)

    #   Locate events list
    events = dir_tree_find(meas_info, FIFF.FIFFB_EVENTS)
    evs = list()
    for event in events:
        ev = dict()
        for k in range(event['nent']):
            kind = event['directory'][k].kind
            pos = event['directory'][k].pos
            if kind == FIFF.FIFF_EVENT_CHANNELS:
                ev['channels'] = read_tag(fid, pos).data
            elif kind == FIFF.FIFF_EVENT_LIST:
                ev['list'] = read_tag(fid, pos).data
        evs.append(ev)
    info['events'] = evs

    #   Locate HPI result
    hpi_results = dir_tree_find(meas_info, FIFF.FIFFB_HPI_RESULT)
    hrs = list()
    for hpi_result in hpi_results:
        hr = dict()
        hr['dig_points'] = []
        for k in range(hpi_result['nent']):
            kind = hpi_result['directory'][k].kind
            pos = hpi_result['directory'][k].pos
            if kind == FIFF.FIFF_DIG_POINT:
                hr['dig_points'].append(read_tag(fid, pos).data)
            elif kind == FIFF.FIFF_HPI_DIGITIZATION_ORDER:
                hr['order'] = read_tag(fid, pos).data
            elif kind == FIFF.FIFF_HPI_COILS_USED:
                hr['used'] = read_tag(fid, pos).data
            elif kind == FIFF.FIFF_HPI_COIL_MOMENTS:
                hr['moments'] = read_tag(fid, pos).data
            elif kind == FIFF.FIFF_HPI_FIT_GOODNESS:
                hr['goodness'] = read_tag(fid, pos).data
            elif kind == FIFF.FIFF_HPI_FIT_GOOD_LIMIT:
                hr['good_limit'] = float(read_tag(fid, pos).data)
            elif kind == FIFF.FIFF_HPI_FIT_DIST_LIMIT:
                hr['dist_limit'] = float(read_tag(fid, pos).data)
            elif kind == FIFF.FIFF_HPI_FIT_ACCEPT:
                hr['accept'] = int(read_tag(fid, pos).data)
            elif kind == FIFF.FIFF_COORD_TRANS:
                hr['coord_trans'] = read_tag(fid, pos).data
        hrs.append(hr)
    info['hpi_results'] = hrs

    #   Locate HPI Measurement
    hpi_meass = dir_tree_find(meas_info, FIFF.FIFFB_HPI_MEAS)
    hms = list()
    for hpi_meas in hpi_meass:
        hm = dict()
        for k in range(hpi_meas['nent']):
            kind = hpi_meas['directory'][k].kind
            pos = hpi_meas['directory'][k].pos
            if kind == FIFF.FIFF_CREATOR:
                hm['creator'] = str(read_tag(fid, pos).data)
            elif kind == FIFF.FIFF_SFREQ:
                hm['sfreq'] = float(read_tag(fid, pos).data)
            elif kind == FIFF.FIFF_NCHAN:
                hm['nchan'] = int(read_tag(fid, pos).data)
            elif kind == FIFF.FIFF_NAVE:
                hm['nave'] = int(read_tag(fid, pos).data)
            elif kind == FIFF.FIFF_HPI_NCOIL:
                hm['ncoil'] = int(read_tag(fid, pos).data)
            elif kind == FIFF.FIFF_FIRST_SAMPLE:
                hm['first_samp'] = int(read_tag(fid, pos).data)
            elif kind == FIFF.FIFF_LAST_SAMPLE:
                hm['last_samp'] = int(read_tag(fid, pos).data)
        hpi_coils = dir_tree_find(hpi_meas, FIFF.FIFFB_HPI_COIL)
        hcs = []
        for hpi_coil in hpi_coils:
            hc = dict()
            for k in range(hpi_coil['nent']):
                kind = hpi_coil['directory'][k].kind
                pos = hpi_coil['directory'][k].pos
                if kind == FIFF.FIFF_HPI_COIL_NO:
                    hc['number'] = int(read_tag(fid, pos).data)
                elif kind == FIFF.FIFF_EPOCH:
                    hc['epoch'] = read_tag(fid, pos).data
                elif kind == FIFF.FIFF_HPI_SLOPES:
                    hc['slopes'] = read_tag(fid, pos).data
                elif kind == FIFF.FIFF_HPI_CORR_COEFF:
                    hc['corr_coeff'] = read_tag(fid, pos).data
                elif kind == FIFF.FIFF_HPI_COIL_FREQ:
                    hc['coil_freq'] = read_tag(fid, pos).data
            hcs.append(hc)
        hm['hpi_coils'] = hcs
        hms.append(hm)
    info['hpi_meas'] = hms
    del hms

    subject_info = dir_tree_find(meas_info, FIFF.FIFFB_SUBJECT)
    si = None
    if len(subject_info) == 1:
        subject_info = subject_info[0]
        si = dict()
        for k in range(subject_info['nent']):
            kind = subject_info['directory'][k].kind
            pos = subject_info['directory'][k].pos
            if kind == FIFF.FIFF_SUBJ_ID:
                tag = read_tag(fid, pos)
                si['id'] = int(tag.data)
            elif kind == FIFF.FIFF_SUBJ_HIS_ID:
                tag = read_tag(fid, pos)
                si['his_id'] = str(tag.data)
            elif kind == FIFF.FIFF_SUBJ_LAST_NAME:
                tag = read_tag(fid, pos)
                si['last_name'] = str(tag.data)
            elif kind == FIFF.FIFF_SUBJ_FIRST_NAME:
                tag = read_tag(fid, pos)
                si['first_name'] = str(tag.data)
            elif kind == FIFF.FIFF_SUBJ_MIDDLE_NAME:
                tag = read_tag(fid, pos)
                si['middle_name'] = str(tag.data)
            elif kind == FIFF.FIFF_SUBJ_BIRTH_DAY:
                tag = read_tag(fid, pos)
                si['birthday'] = tag.data
            elif kind == FIFF.FIFF_SUBJ_SEX:
                tag = read_tag(fid, pos)
                si['sex'] = int(tag.data)
            elif kind == FIFF.FIFF_SUBJ_HAND:
                tag = read_tag(fid, pos)
                si['hand'] = int(tag.data)
            elif kind == FIFF.FIFF_SUBJ_WEIGHT:
                tag = read_tag(fid, pos)
                si['weight'] = tag.data
            elif kind == FIFF.FIFF_SUBJ_HEIGHT:
                tag = read_tag(fid, pos)
                si['height'] = tag.data
    info['subject_info'] = si
    del si

    device_info = dir_tree_find(meas_info, FIFF.FIFFB_DEVICE)
    di = None
    if len(device_info) == 1:
        device_info = device_info[0]
        di = dict()
        for k in range(device_info['nent']):
            kind = device_info['directory'][k].kind
            pos = device_info['directory'][k].pos
            if kind == FIFF.FIFF_DEVICE_TYPE:
                tag = read_tag(fid, pos)
                di['type'] = str(tag.data)
            elif kind == FIFF.FIFF_DEVICE_MODEL:
                tag = read_tag(fid, pos)
                di['model'] = str(tag.data)
            elif kind == FIFF.FIFF_DEVICE_SERIAL:
                tag = read_tag(fid, pos)
                di['serial'] = str(tag.data)
            elif kind == FIFF.FIFF_DEVICE_SITE:
                tag = read_tag(fid, pos)
                di['site'] = str(tag.data)
    info['device_info'] = di
    del di

    helium_info = dir_tree_find(meas_info, FIFF.FIFFB_HELIUM)
    hi = None
    if len(helium_info) == 1:
        helium_info = helium_info[0]
        hi = dict()
        for k in range(helium_info['nent']):
            kind = helium_info['directory'][k].kind
            pos = helium_info['directory'][k].pos
            if kind == FIFF.FIFF_HE_LEVEL_RAW:
                tag = read_tag(fid, pos)
                hi['he_level_raw'] = float(tag.data)
            elif kind == FIFF.FIFF_HELIUM_LEVEL:
                tag = read_tag(fid, pos)
                hi['helium_level'] = float(tag.data)
            elif kind == FIFF.FIFF_ORIG_FILE_GUID:
                tag = read_tag(fid, pos)
                hi['orig_file_guid'] = str(tag.data)
            elif kind == FIFF.FIFF_MEAS_DATE:
                tag = read_tag(fid, pos)
                hi['meas_date'] = tuple(int(t) for t in tag.data)
    info['helium_info'] = hi
    del hi

    hpi_subsystem = dir_tree_find(meas_info, FIFF.FIFFB_HPI_SUBSYSTEM)
    hs = None
    if len(hpi_subsystem) == 1:
        hpi_subsystem = hpi_subsystem[0]
        hs = dict()
        for k in range(hpi_subsystem['nent']):
            kind = hpi_subsystem['directory'][k].kind
            pos = hpi_subsystem['directory'][k].pos
            if kind == FIFF.FIFF_HPI_NCOIL:
                tag = read_tag(fid, pos)
                hs['ncoil'] = int(tag.data)
            elif kind == FIFF.FIFF_EVENT_CHANNEL:
                tag = read_tag(fid, pos)
                hs['event_channel'] = str(tag.data)
            hpi_coils = dir_tree_find(hpi_subsystem, FIFF.FIFFB_HPI_COIL)
            hc = []
            for coil in hpi_coils:
                this_coil = dict()
                for j in range(coil['nent']):
                    kind = coil['directory'][j].kind
                    pos = coil['directory'][j].pos
                    if kind == FIFF.FIFF_EVENT_BITS:
                        tag = read_tag(fid, pos)
                        this_coil['event_bits'] = np.array(tag.data)
                hc.append(this_coil)
            hs['hpi_coils'] = hc
    info['hpi_subsystem'] = hs

    #   Read processing history
    info['proc_history'] = _read_proc_history(fid, tree)

    #  Make the most appropriate selection for the measurement id
    if meas_info['parent_id'] is None:
        if meas_info['id'] is None:
            if meas['id'] is None:
                if meas['parent_id'] is None:
                    info['meas_id'] = info['file_id']
                else:
                    info['meas_id'] = meas['parent_id']
            else:
                info['meas_id'] = meas['id']
        else:
            info['meas_id'] = meas_info['id']
    else:
        info['meas_id'] = meas_info['parent_id']
    info['experimenter'] = experimenter
    info['description'] = description
    info['proj_id'] = proj_id
    info['proj_name'] = proj_name
    if meas_date is None:
        meas_date = (info['meas_id']['secs'], info['meas_id']['usecs'])
    if np.array_equal(meas_date, DATE_NONE):
        meas_date = None
    else:
        meas_date = _stamp_to_dt(meas_date)
    info['meas_date'] = meas_date
    info['utc_offset'] = utc_offset

    info['sfreq'] = sfreq
    info['highpass'] = highpass if highpass is not None else 0.
    info['lowpass'] = lowpass if lowpass is not None else info['sfreq'] / 2.0
    info['line_freq'] = line_freq
    info['gantry_angle'] = gantry_angle

    #   Add the channel information and make a list of channel names
    #   for convenience
    info['chs'] = chs

    #
    #  Add the coordinate transformations
    #
    info['dev_head_t'] = dev_head_t
    info['ctf_head_t'] = ctf_head_t
    info['dev_ctf_t'] = dev_ctf_t
    if dev_head_t is not None and ctf_head_t is not None and dev_ctf_t is None:
        from ..transforms import Transform
        head_ctf_trans = linalg.inv(ctf_head_t['trans'])
        dev_ctf_trans = np.dot(head_ctf_trans, info['dev_head_t']['trans'])
        info['dev_ctf_t'] = Transform('meg', 'ctf_head', dev_ctf_trans)

    #   All kinds of auxliary stuff
    info['dig'] = _format_dig_points(dig)
    info['bads'] = bads
    info._update_redundant()
    if clean_bads:
        info['bads'] = [b for b in bads if b in info['ch_names']]
    info['projs'] = projs
    info['comps'] = comps
    info['acq_pars'] = acq_pars
    info['acq_stim'] = acq_stim
    info['custom_ref_applied'] = custom_ref_applied
    info['xplotter_layout'] = xplotter_layout
    info['kit_system_id'] = kit_system_id
    info._check_consistency()
    return info, meas


def write_meas_info(fid, info, data_type=None, reset_range=True):
    """Write measurement info into a file id (from a fif file).

    Parameters
    ----------
    fid : file
        Open file descriptor.
    info : instance of Info
        The measurement info structure.
    data_type : int
        The data_type in case it is necessary. Should be 4 (FIFFT_FLOAT),
        5 (FIFFT_DOUBLE), or 16 (FIFFT_DAU_PACK16) for
        raw data.
    reset_range : bool
        If True, info['chs'][k]['range'] will be set to unity.

    Notes
    -----
    Tags are written in a particular order for compatibility with maxfilter.
    """
    info._check_consistency()

    # Measurement info
    start_block(fid, FIFF.FIFFB_MEAS_INFO)

    # Add measurement id
    if info['meas_id'] is not None:
        write_id(fid, FIFF.FIFF_PARENT_BLOCK_ID, info['meas_id'])

    for event in info['events']:
        start_block(fid, FIFF.FIFFB_EVENTS)
        if event.get('channels') is not None:
            write_int(fid, FIFF.FIFF_EVENT_CHANNELS, event['channels'])
        if event.get('list') is not None:
            write_int(fid, FIFF.FIFF_EVENT_LIST, event['list'])
        end_block(fid, FIFF.FIFFB_EVENTS)

    #   HPI Result
    for hpi_result in info['hpi_results']:
        start_block(fid, FIFF.FIFFB_HPI_RESULT)
        write_dig_points(fid, hpi_result['dig_points'])
        if 'order' in hpi_result:
            write_int(fid, FIFF.FIFF_HPI_DIGITIZATION_ORDER,
                      hpi_result['order'])
        if 'used' in hpi_result:
            write_int(fid, FIFF.FIFF_HPI_COILS_USED, hpi_result['used'])
        if 'moments' in hpi_result:
            write_float_matrix(fid, FIFF.FIFF_HPI_COIL_MOMENTS,
                               hpi_result['moments'])
        if 'goodness' in hpi_result:
            write_float(fid, FIFF.FIFF_HPI_FIT_GOODNESS,
                        hpi_result['goodness'])
        if 'good_limit' in hpi_result:
            write_float(fid, FIFF.FIFF_HPI_FIT_GOOD_LIMIT,
                        hpi_result['good_limit'])
        if 'dist_limit' in hpi_result:
            write_float(fid, FIFF.FIFF_HPI_FIT_DIST_LIMIT,
                        hpi_result['dist_limit'])
        if 'accept' in hpi_result:
            write_int(fid, FIFF.FIFF_HPI_FIT_ACCEPT, hpi_result['accept'])
        if 'coord_trans' in hpi_result:
            write_coord_trans(fid, hpi_result['coord_trans'])
        end_block(fid, FIFF.FIFFB_HPI_RESULT)

    #   HPI Measurement
    for hpi_meas in info['hpi_meas']:
        start_block(fid, FIFF.FIFFB_HPI_MEAS)
        if hpi_meas.get('creator') is not None:
            write_string(fid, FIFF.FIFF_CREATOR, hpi_meas['creator'])
        if hpi_meas.get('sfreq') is not None:
            write_float(fid, FIFF.FIFF_SFREQ, hpi_meas['sfreq'])
        if hpi_meas.get('nchan') is not None:
            write_int(fid, FIFF.FIFF_NCHAN, hpi_meas['nchan'])
        if hpi_meas.get('nave') is not None:
            write_int(fid, FIFF.FIFF_NAVE, hpi_meas['nave'])
        if hpi_meas.get('ncoil') is not None:
            write_int(fid, FIFF.FIFF_HPI_NCOIL, hpi_meas['ncoil'])
        if hpi_meas.get('first_samp') is not None:
            write_int(fid, FIFF.FIFF_FIRST_SAMPLE, hpi_meas['first_samp'])
        if hpi_meas.get('last_samp') is not None:
            write_int(fid, FIFF.FIFF_LAST_SAMPLE, hpi_meas['last_samp'])
        for hpi_coil in hpi_meas['hpi_coils']:
            start_block(fid, FIFF.FIFFB_HPI_COIL)
            if hpi_coil.get('number') is not None:
                write_int(fid, FIFF.FIFF_HPI_COIL_NO, hpi_coil['number'])
            if hpi_coil.get('epoch') is not None:
                write_float_matrix(fid, FIFF.FIFF_EPOCH, hpi_coil['epoch'])
            if hpi_coil.get('slopes') is not None:
                write_float(fid, FIFF.FIFF_HPI_SLOPES, hpi_coil['slopes'])
            if hpi_coil.get('corr_coeff') is not None:
                write_float(fid, FIFF.FIFF_HPI_CORR_COEFF,
                            hpi_coil['corr_coeff'])
            if hpi_coil.get('coil_freq') is not None:
                write_float(fid, FIFF.FIFF_HPI_COIL_FREQ,
                            hpi_coil['coil_freq'])
            end_block(fid, FIFF.FIFFB_HPI_COIL)
        end_block(fid, FIFF.FIFFB_HPI_MEAS)

    #   Polhemus data
    write_dig_points(fid, info['dig'], block=True)

    #   megacq parameters
    if info['acq_pars'] is not None or info['acq_stim'] is not None:
        start_block(fid, FIFF.FIFFB_DACQ_PARS)
        if info['acq_pars'] is not None:
            write_string(fid, FIFF.FIFF_DACQ_PARS, info['acq_pars'])

        if info['acq_stim'] is not None:
            write_string(fid, FIFF.FIFF_DACQ_STIM, info['acq_stim'])

        end_block(fid, FIFF.FIFFB_DACQ_PARS)

    #   Coordinate transformations if the HPI result block was not there
    if info['dev_head_t'] is not None:
        write_coord_trans(fid, info['dev_head_t'])

    if info['ctf_head_t'] is not None:
        write_coord_trans(fid, info['ctf_head_t'])

    if info['dev_ctf_t'] is not None:
        write_coord_trans(fid, info['dev_ctf_t'])

    #   Projectors
    _write_proj(fid, info['projs'])

    #   Bad channels
    if len(info['bads']) > 0:
        start_block(fid, FIFF.FIFFB_MNE_BAD_CHANNELS)
        write_name_list(fid, FIFF.FIFF_MNE_CH_NAME_LIST, info['bads'])
        end_block(fid, FIFF.FIFFB_MNE_BAD_CHANNELS)

    #   General
    if info.get('experimenter') is not None:
        write_string(fid, FIFF.FIFF_EXPERIMENTER, info['experimenter'])
    if info.get('description') is not None:
        write_string(fid, FIFF.FIFF_DESCRIPTION, info['description'])
    if info.get('proj_id') is not None:
        write_int(fid, FIFF.FIFF_PROJ_ID, info['proj_id'])
    if info.get('proj_name') is not None:
        write_string(fid, FIFF.FIFF_PROJ_NAME, info['proj_name'])
    if info.get('meas_date') is not None:
        write_int(fid, FIFF.FIFF_MEAS_DATE, _dt_to_stamp(info['meas_date']))
    if info.get('utc_offset') is not None:
        write_string(fid, FIFF.FIFF_UTC_OFFSET, info['utc_offset'])
    write_int(fid, FIFF.FIFF_NCHAN, info['nchan'])
    write_float(fid, FIFF.FIFF_SFREQ, info['sfreq'])
    if info['lowpass'] is not None:
        write_float(fid, FIFF.FIFF_LOWPASS, info['lowpass'])
    if info['highpass'] is not None:
        write_float(fid, FIFF.FIFF_HIGHPASS, info['highpass'])
    if info.get('line_freq') is not None:
        write_float(fid, FIFF.FIFF_LINE_FREQ, info['line_freq'])
    if info.get('gantry_angle') is not None:
        write_float(fid, FIFF.FIFF_GANTRY_ANGLE, info['gantry_angle'])
    if data_type is not None:
        write_int(fid, FIFF.FIFF_DATA_PACK, data_type)
    if info.get('custom_ref_applied'):
        write_int(fid, FIFF.FIFF_MNE_CUSTOM_REF, info['custom_ref_applied'])
    if info.get('xplotter_layout'):
        write_string(fid, FIFF.FIFF_XPLOTTER_LAYOUT, info['xplotter_layout'])

    #  Channel information
    for k, c in enumerate(info['chs']):
        #   Scan numbers may have been messed up
        c = deepcopy(c)
        c['scanno'] = k + 1
        # for float/double, the "range" param is unnecessary
        if reset_range is True:
            c['range'] = 1.0
        write_ch_info(fid, c)

    # Subject information
    if info.get('subject_info') is not None:
        start_block(fid, FIFF.FIFFB_SUBJECT)
        si = info['subject_info']
        if si.get('id') is not None:
            write_int(fid, FIFF.FIFF_SUBJ_ID, si['id'])
        if si.get('his_id') is not None:
            write_string(fid, FIFF.FIFF_SUBJ_HIS_ID, si['his_id'])
        if si.get('last_name') is not None:
            write_string(fid, FIFF.FIFF_SUBJ_LAST_NAME, si['last_name'])
        if si.get('first_name') is not None:
            write_string(fid, FIFF.FIFF_SUBJ_FIRST_NAME, si['first_name'])
        if si.get('middle_name') is not None:
            write_string(fid, FIFF.FIFF_SUBJ_MIDDLE_NAME, si['middle_name'])
        if si.get('birthday') is not None:
            write_julian(fid, FIFF.FIFF_SUBJ_BIRTH_DAY, si['birthday'])
        if si.get('sex') is not None:
            write_int(fid, FIFF.FIFF_SUBJ_SEX, si['sex'])
        if si.get('hand') is not None:
            write_int(fid, FIFF.FIFF_SUBJ_HAND, si['hand'])
        if si.get('weight') is not None:
            write_float(fid, FIFF.FIFF_SUBJ_WEIGHT, si['weight'])
        if si.get('height') is not None:
            write_float(fid, FIFF.FIFF_SUBJ_HEIGHT, si['height'])
        end_block(fid, FIFF.FIFFB_SUBJECT)
        del si

    if info.get('device_info') is not None:
        start_block(fid, FIFF.FIFFB_DEVICE)
        di = info['device_info']
        write_string(fid, FIFF.FIFF_DEVICE_TYPE, di['type'])
        for key in ('model', 'serial', 'site'):
            if di.get(key) is not None:
                write_string(fid, getattr(FIFF, 'FIFF_DEVICE_' + key.upper()),
                             di[key])
        end_block(fid, FIFF.FIFFB_DEVICE)
        del di

    if info.get('helium_info') is not None:
        start_block(fid, FIFF.FIFFB_HELIUM)
        hi = info['helium_info']
        if hi.get('he_level_raw') is not None:
            write_float(fid, FIFF.FIFF_HE_LEVEL_RAW, hi['he_level_raw'])
        if hi.get('helium_level') is not None:
            write_float(fid, FIFF.FIFF_HELIUM_LEVEL, hi['helium_level'])
        if hi.get('orig_file_guid') is not None:
            write_string(fid, FIFF.FIFF_ORIG_FILE_GUID, hi['orig_file_guid'])
        write_int(fid, FIFF.FIFF_MEAS_DATE, hi['meas_date'])
        end_block(fid, FIFF.FIFFB_HELIUM)
        del hi

    if info.get('hpi_subsystem') is not None:
        hs = info['hpi_subsystem']
        start_block(fid, FIFF.FIFFB_HPI_SUBSYSTEM)
        if hs.get('ncoil') is not None:
            write_int(fid, FIFF.FIFF_HPI_NCOIL, hs['ncoil'])
        if hs.get('event_channel') is not None:
            write_string(fid, FIFF.FIFF_EVENT_CHANNEL, hs['event_channel'])
        if hs.get('hpi_coils') is not None:
            for coil in hs['hpi_coils']:
                start_block(fid, FIFF.FIFFB_HPI_COIL)
                if coil.get('event_bits') is not None:
                    write_int(fid, FIFF.FIFF_EVENT_BITS,
                              coil['event_bits'])
                end_block(fid, FIFF.FIFFB_HPI_COIL)
        end_block(fid, FIFF.FIFFB_HPI_SUBSYSTEM)
        del hs

    #   CTF compensation info
    write_ctf_comp(fid, info['comps'])

    #   KIT system ID
    if info.get('kit_system_id') is not None:
        write_int(fid, FIFF.FIFF_MNE_KIT_SYSTEM_ID, info['kit_system_id'])

    end_block(fid, FIFF.FIFFB_MEAS_INFO)

    #   Processing history
    _write_proc_history(fid, info)


def write_info(fname, info, data_type=None, reset_range=True):
    """Write measurement info in fif file.

    Parameters
    ----------
    fname : str
        The name of the file. Should end by -info.fif.
    info : instance of Info
        The measurement info structure.
    data_type : int
        The data_type in case it is necessary. Should be 4 (FIFFT_FLOAT),
        5 (FIFFT_DOUBLE), or 16 (FIFFT_DAU_PACK16) for
        raw data.
    reset_range : bool
        If True, info['chs'][k]['range'] will be set to unity.
    """
    fid = start_file(fname)
    start_block(fid, FIFF.FIFFB_MEAS)
    write_meas_info(fid, info, data_type, reset_range)
    end_block(fid, FIFF.FIFFB_MEAS)
    end_file(fid)


@verbose
def _merge_info_values(infos, key, verbose=None):
    """Merge things together.

    Fork for {'dict', 'list', 'array', 'other'}
    and consider cases where one or all are of the same type.

    Does special things for "projs", "bads", and "meas_date".
    """
    values = [d[key] for d in infos]
    msg = ("Don't know how to merge '%s'. Make sure values are "
           "compatible, got types:\n    %s"
           % (key, [type(v) for v in values]))

    def _flatten(lists):
        return [item for sublist in lists for item in sublist]

    def _check_isinstance(values, kind, func):
        return func([isinstance(v, kind) for v in values])

    def _where_isinstance(values, kind):
        """Get indices of instances."""
        return np.where([isinstance(v, type) for v in values])[0]

    # list
    if _check_isinstance(values, list, all):
        lists = (d[key] for d in infos)
        if key == 'projs':
            return _uniquify_projs(_flatten(lists))
        elif key == 'bads':
            return sorted(set(_flatten(lists)))
        else:
            return _flatten(lists)
    elif _check_isinstance(values, list, any):
        idx = _where_isinstance(values, list)
        if len(idx) == 1:
            return values[int(idx)]
        elif len(idx) > 1:
            lists = (d[key] for d in infos if isinstance(d[key], list))
            return _flatten(lists)
    # dict
    elif _check_isinstance(values, dict, all):
        is_qual = all(object_diff(values[0], v) == '' for v in values[1:])
        if is_qual:
            return values[0]
        else:
            RuntimeError(msg)
    elif _check_isinstance(values, dict, any):
        idx = _where_isinstance(values, dict)
        if len(idx) == 1:
            return values[int(idx)]
        elif len(idx) > 1:
            raise RuntimeError(msg)
    # ndarray
    elif _check_isinstance(values, np.ndarray, all) or \
            _check_isinstance(values, tuple, all):
        is_qual = all(np.array_equal(values[0], x) for x in values[1:])
        if is_qual:
            return values[0]
        elif key == 'meas_date':
            logger.info('Found multiple entries for %s. '
                        'Setting value to `None`' % key)
            return None
        else:
            raise RuntimeError(msg)
    elif _check_isinstance(values, (np.ndarray, tuple), any):
        idx = _where_isinstance(values, np.ndarray)
        if len(idx) == 1:
            return values[int(idx)]
        elif len(idx) > 1:
            raise RuntimeError(msg)
    # other
    else:
        unique_values = set(values)
        if len(unique_values) == 1:
            return list(values)[0]
        elif isinstance(list(unique_values)[0], BytesIO):
            logger.info('Found multiple StringIO instances. '
                        'Setting value to `None`')
            return None
        elif isinstance(list(unique_values)[0], str):
            logger.info('Found multiple filenames. '
                        'Setting value to `None`')
            return None
        else:
            raise RuntimeError(msg)


@verbose
def _merge_info(infos, force_update_to_first=False, verbose=None):
    """Merge multiple measurement info dictionaries.

     - Fields that are present in only one info object will be used in the
       merged info.
     - Fields that are present in multiple info objects and are the same
       will be used in the merged info.
     - Fields that are present in multiple info objects and are different
       will result in a None value in the merged info.
     - Channels will be concatenated. If multiple info objects contain
       channels with the same name, an exception is raised.

    Parameters
    ----------
    infos | list of instance of Info
        Info objects to merge into one info object.
    force_update_to_first : bool
        If True, force the fields for objects in `info` will be updated
        to match those in the first item. Use at your own risk, as this
        may overwrite important metadata.
    %(verbose)s

    Returns
    -------
    info : instance of Info
        The merged info object.
    """
    for info in infos:
        info._check_consistency()
    if force_update_to_first is True:
        infos = deepcopy(infos)
        _force_update_info(infos[0], infos[1:])
    info = Info()
    info['chs'] = []
    for this_info in infos:
        info['chs'].extend(this_info['chs'])
    info._update_redundant()
    duplicates = {ch for ch in info['ch_names']
                  if info['ch_names'].count(ch) > 1}
    if len(duplicates) > 0:
        msg = ("The following channels are present in more than one input "
               "measurement info objects: %s" % list(duplicates))
        raise ValueError(msg)

    transforms = ['ctf_head_t', 'dev_head_t', 'dev_ctf_t']
    for trans_name in transforms:
        trans = [i[trans_name] for i in infos if i[trans_name]]
        if len(trans) == 0:
            info[trans_name] = None
        elif len(trans) == 1:
            info[trans_name] = trans[0]
        elif all(np.all(trans[0]['trans'] == x['trans']) and
                 trans[0]['from'] == x['from'] and
                 trans[0]['to'] == x['to']
                 for x in trans[1:]):
            info[trans_name] = trans[0]
        else:
            msg = ("Measurement infos provide mutually inconsistent %s" %
                   trans_name)
            raise ValueError(msg)

    # KIT system-IDs
    kit_sys_ids = [i['kit_system_id'] for i in infos if i['kit_system_id']]
    if len(kit_sys_ids) == 0:
        info['kit_system_id'] = None
    elif len(set(kit_sys_ids)) == 1:
        info['kit_system_id'] = kit_sys_ids[0]
    else:
        raise ValueError("Trying to merge channels from different KIT systems")

    # hpi infos and digitization data:
    fields = ['hpi_results', 'hpi_meas', 'dig']
    for k in fields:
        values = [i[k] for i in infos if i[k]]
        if len(values) == 0:
            info[k] = []
        elif len(values) == 1:
            info[k] = values[0]
        elif all(object_diff(values[0], v) == '' for v in values[1:]):
            info[k] = values[0]
        else:
            msg = ("Measurement infos are inconsistent for %s" % k)
            raise ValueError(msg)

    # other fields
    other_fields = ['acq_pars', 'acq_stim', 'bads',
                    'comps', 'custom_ref_applied', 'description',
                    'experimenter', 'file_id', 'highpass', 'utc_offset',
                    'hpi_subsystem', 'events', 'device_info', 'helium_info',
                    'line_freq', 'lowpass', 'meas_id',
                    'proj_id', 'proj_name', 'projs', 'sfreq', 'gantry_angle',
                    'subject_info', 'sfreq', 'xplotter_layout', 'proc_history']
    for k in other_fields:
        info[k] = _merge_info_values(infos, k)

    info['meas_date'] = infos[0]['meas_date']
    info._check_consistency()
    return info


@verbose
def create_info(ch_names, sfreq, ch_types=None, montage=None, verbose=None):
    """Create a basic Info instance suitable for use with create_raw.

    Parameters
    ----------
    ch_names : list of str | int
        Channel names. If an int, a list of channel names will be created
        from ``range(ch_names)``.
    sfreq : float
        Sample rate of the data.
    ch_types : list of str | str
        Channel types. If None, data are assumed to be misc.
        Currently supported fields are 'ecg', 'bio', 'stim', 'eog', 'misc',
        'seeg', 'ecog', 'mag', 'eeg', 'ref_meg', 'grad', 'emg', 'hbr' or 'hbo'.
        If str, then all channels are assumed to be of the same type.
    %(montage)s
    %(verbose)s

    Returns
    -------
    info : instance of Info
        The measurement info.

    Notes
    -----
    The info dictionary will be sparsely populated to enable functionality
    within the rest of the package. Advanced functionality such as source
    localization can only be obtained through substantial, proper
    modifications of the info structure (not recommended).

    Note that the MEG device-to-head transform ``info['dev_head_t']`` will
    be initialized to the identity transform.

    Proper units of measure:
    * V: eeg, eog, seeg, emg, ecg, bio, ecog
    * T: mag
    * T/m: grad
    * M: hbo, hbr
    * Am: dipole
    * AU: misc
    """
    from ..channels.montage import (DigMontage, _set_montage)
    try:
        ch_names = operator.index(ch_names)  # int-like
    except TypeError:
        pass
    else:
        ch_names = list(np.arange(ch_names).astype(str))
    _validate_type(ch_names, (list, tuple), "ch_names",
                   ("list, tuple, or int"))
    _validate_type(montage, types=(type(None), str, DigMontage),
                   item_name='montage')
    sfreq = float(sfreq)
    if sfreq <= 0:
        raise ValueError('sfreq must be positive')
    nchan = len(ch_names)
    if ch_types is None:
        ch_types = ['misc'] * nchan
    if isinstance(ch_types, str):
        ch_types = [ch_types] * nchan
    ch_types = np.atleast_1d(np.array(ch_types, np.str))
    if ch_types.ndim != 1 or len(ch_types) != nchan:
        raise ValueError('ch_types and ch_names must be the same length '
                         '(%s != %s) for ch_types=%s'
                         % (len(ch_types), nchan, ch_types))
    info = _empty_info(sfreq)
    for ci, (name, kind) in enumerate(zip(ch_names, ch_types)):
        _validate_type(name, 'str', "each entry in ch_names")
        _validate_type(kind, 'str', "each entry in ch_types")
        if kind not in _kind_dict:
            raise KeyError('kind must be one of %s, not %s'
                           % (list(_kind_dict.keys()), kind))
        kind = _kind_dict[kind]
        # mirror what tag.py does here
        coord_frame = _coord_dict.get(kind[0], FIFF.FIFFV_COORD_UNKNOWN)
        chan_info = dict(loc=np.full(12, np.nan), unit_mul=0, range=1., cal=1.,
                         kind=kind[0], coil_type=kind[1],
                         unit=kind[2], coord_frame=coord_frame,
                         ch_name=str(name), scanno=ci + 1, logno=ci + 1)
        info['chs'].append(chan_info)

    info._update_redundant()
    _set_montage(info, montage)
    info._check_consistency()
    return info


RAW_INFO_FIELDS = (
    'acq_pars', 'acq_stim', 'bads', 'ch_names', 'chs',
    'comps', 'ctf_head_t', 'custom_ref_applied', 'description', 'dev_ctf_t',
    'dev_head_t', 'dig', 'experimenter', 'events', 'utc_offset', 'device_info',
    'file_id', 'highpass', 'hpi_meas', 'hpi_results', 'helium_info',
    'hpi_subsystem', 'kit_system_id', 'line_freq', 'lowpass', 'meas_date',
    'meas_id', 'nchan', 'proj_id', 'proj_name', 'projs', 'sfreq',
    'subject_info', 'xplotter_layout', 'proc_history', 'gantry_angle',
)


def _empty_info(sfreq):
    """Create an empty info dictionary."""
    _none_keys = (
        'acq_pars', 'acq_stim', 'ctf_head_t', 'description',
        'dev_ctf_t', 'dig', 'experimenter', 'utc_offset', 'device_info',
        'file_id', 'highpass', 'hpi_subsystem', 'kit_system_id', 'helium_info',
        'line_freq', 'lowpass', 'meas_date', 'meas_id', 'proj_id', 'proj_name',
        'subject_info', 'xplotter_layout', 'gantry_angle',
    )
    _list_keys = ('bads', 'chs', 'comps', 'events', 'hpi_meas', 'hpi_results',
                  'projs', 'proc_history')
    info = Info()
    for k in _none_keys:
        info[k] = None
    for k in _list_keys:
        info[k] = list()
    info['custom_ref_applied'] = FIFF.FIFFV_MNE_CUSTOM_REF_OFF
    info['highpass'] = 0.
    info['sfreq'] = float(sfreq)
    info['lowpass'] = info['sfreq'] / 2.
    info['dev_head_t'] = Transform('meg', 'head')
    info._update_redundant()
    info._check_consistency()
    return info


def _force_update_info(info_base, info_target):
    """Update target info objects with values from info base.

    Note that values in info_target will be overwritten by those in info_base.
    This will overwrite all fields except for: 'chs', 'ch_names', 'nchan'.

    Parameters
    ----------
    info_base : mne.Info
        The Info object you want to use for overwriting values
        in target Info objects.
    info_target : mne.Info | list of mne.Info
        The Info object(s) you wish to overwrite using info_base. These objects
        will be modified in-place.
    """
    exclude_keys = ['chs', 'ch_names', 'nchan']
    info_target = np.atleast_1d(info_target).ravel()
    all_infos = np.hstack([info_base, info_target])
    for ii in all_infos:
        if not isinstance(ii, Info):
            raise ValueError('Inputs must be of type Info. '
                             'Found type %s' % type(ii))
    for key, val in info_base.items():
        if key in exclude_keys:
            continue
        for i_targ in info_target:
            i_targ[key] = val


def _add_timedelta_to_stamp(meas_date_stamp, delta_t):
    """Add a timedelta to a meas_date tuple."""
    if meas_date_stamp is not None:
        meas_date_stamp = _dt_to_stamp(_stamp_to_dt(meas_date_stamp) + delta_t)
    return meas_date_stamp


@verbose
def anonymize_info(info, daysback=None, keep_his=False, verbose=None):
    """Anonymize measurement information in place.

    Parameters
    ----------
    info : dict, instance of Info
        Measurement information for the dataset.
    daysback : int | None
        Number of days to subtract from all dates.
        If None (default) the date of service will be set to Jan 1ˢᵗ 2000.
        This parameter is ignored if ``info['meas_date'] is None``.
    keep_his : bool
        If True his_id of subject_info will NOT be overwritten.
        Defaults to False.

        .. warning:: This could mean that ``info`` is not fully
                     anonymized. Use with caution.
    %(verbose)s

    Returns
    -------
    info : instance of Info
        Measurement information for the dataset.

    Notes
    -----
    Removes potentially identifying information if it exist in ``info``.
    Specifically for each of the following we use:

    - meas_date, file_id, meas_id
          A default value, or as specified by ``daysback``.
    - subject_info
          Default values, except for 'birthday' which is adjusted
          to maintain the subject age.
    - experimenter, proj_name, description
          Default strings.
    - utc_offset
          ``None``.
    - proj_id
          Zeros.
    - proc_history
          Dates use the meas_date logic, and experimenter a default string.
    - helium_info, device_info
          Dates use the meas_date logic, meta info uses defaults.

    If ``info['meas_date']`` is None, it will remain None during processing
    the above fields.

    Operates in place.
    """
    _validate_type(info, 'info', "self")

    default_anon_dos = datetime.datetime(2000, 1, 1, 0, 0, 0,
                                         tzinfo=datetime.timezone.utc)
    default_str = "mne_anonymize"
    default_subject_id = 0
    default_desc = ("Anonymized using a time shift"
                    " to preserve age at acquisition")

    none_meas_date = info['meas_date'] is None

    if none_meas_date:
        warn('Input info has \'meas_date\' set to None.'
             ' Removing all information from time/date structures.'
             ' *NOT* performing any time shifts')
        info['meas_date'] = None
    else:
        # compute timeshift delta
        if daysback is None:
            delta_t = info['meas_date'] - default_anon_dos
        else:
            delta_t = datetime.timedelta(days=daysback)
        # adjust meas_date
        info['meas_date'] = info['meas_date'] - delta_t

    # file_id and meas_id
    for key in ('file_id', 'meas_id'):
        value = info.get(key)
        if value is not None:
            assert 'msecs' not in value
            if none_meas_date:
                tmp = DATE_NONE
            else:
                tmp = _add_timedelta_to_stamp(
                    (value['secs'], value['usecs']), -delta_t)
            value['secs'] = tmp[0]
            value['usecs'] = tmp[1]
            # The following copy is needed for a test CTF dataset
            # otherwise value['machid'][:] = 0 would suffice
            _tmp = value['machid'].copy()
            _tmp[:] = 0
            value['machid'] = _tmp

    # subject info
    subject_info = info.get('subject_info')
    if subject_info is not None:
        if subject_info.get('id') is not None:
            subject_info['id'] = default_subject_id
        if keep_his:
            logger.info('Not fully anonymizing info - keeping \'his_id\'')
        elif subject_info.get('his_id') is not None:
            subject_info['his_id'] = str(default_subject_id)

        for key in ('last_name', 'first_name', 'middle_name'):
            if subject_info.get(key) is not None:
                subject_info[key] = default_str

        # anonymize the subject birthday
        if none_meas_date:
            subject_info.pop('birthday', None)
        elif subject_info.get('birthday') is not None:
            dob = datetime.datetime(subject_info['birthday'][0],
                                    subject_info['birthday'][1],
                                    subject_info['birthday'][2])
            dob -= delta_t
            subject_info['birthday'] = dob.year, dob.month, dob.day

        for key in ('weight', 'height'):
            if subject_info.get(key) is not None:
                subject_info[key] = 0

    info['experimenter'] = default_str
    info['description'] = default_desc

    if info['proj_id'] is not None:
        info['proj_id'] = np.zeros_like(info['proj_id'])
    if info['proj_name'] is not None:
        info['proj_name'] = default_str
    if info['utc_offset'] is not None:
        info['utc_offset'] = None

    proc_hist = info.get('proc_history')
    if proc_hist is not None:
        for record in proc_hist:
            record['block_id']['machid'][:] = 0
            record['experimenter'] = default_str
            if none_meas_date:
                record['block_id']['secs'] = DATE_NONE[0]
                record['block_id']['usecs'] = DATE_NONE[1]
                record['date'] = DATE_NONE
            else:
                this_t0 = (record['block_id']['secs'],
                           record['block_id']['usecs'])
                this_t1 = _add_timedelta_to_stamp(
                    this_t0, -delta_t)
                record['block_id']['secs'] = this_t1[0]
                record['block_id']['usecs'] = this_t1[1]
                record['date'] = _add_timedelta_to_stamp(
                    record['date'], -delta_t)

    hi = info.get('helium_info')
    if hi is not None:
        if hi.get('orig_file_guid') is not None:
            hi['orig_file_guid'] = default_str
        if none_meas_date and hi.get('meas_date') is not None:
            hi['meas_date'] = DATE_NONE
        elif hi.get('meas_date') is not None:
            hi['meas_date'] = _add_timedelta_to_stamp(
                hi['meas_date'], -delta_t)

    di = info.get('device_info')
    if di is not None:
        for k in ('serial', 'site'):
            if di.get(k) is not None:
                di[k] = default_str

    err_mesg = ('anonymize_info generated an inconsistent info object. Most '
                'often this is because daysback parameter was too large. '
                'Underlying Error:\n')
    info._check_consistency(prepend_error=err_mesg)

    return info


def _bad_chans_comp(info, ch_names):
    """Check if channel names are consistent with current compensation status.

    Parameters
    ----------
    info : dict, instance of Info
        Measurement information for the dataset.

    ch_names : list of str
        The channel names to check.

    Returns
    -------
    status : bool
        True if compensation is *currently* in use but some compensation
            channels are not included in picks

        False if compensation is *currently* not being used
            or if compensation is being used and all compensation channels
            in info and included in picks.

    missing_ch_names: array-like of str, shape (n_missing,)
        The names of compensation channels not included in picks.
        Returns [] if no channels are missing.

    """
    if 'comps' not in info:
        # should this be thought of as a bug?
        return False, []

    # only include compensation channels that would affect selected channels
    ch_names_s = set(ch_names)
    comp_names = []
    for comp in info['comps']:
        if len(ch_names_s.intersection(comp['data']['row_names'])) > 0:
            comp_names.extend(comp['data']['col_names'])
    comp_names = sorted(set(comp_names))

    missing_ch_names = sorted(set(comp_names).difference(ch_names))

    if get_current_comp(info) != 0 and len(missing_ch_names) > 0:
        return True, missing_ch_names

    return False, missing_ch_names
