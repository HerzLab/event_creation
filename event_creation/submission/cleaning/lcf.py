import os
import mne
import numpy as np
import scipy.signal as sp_signal
from ptsa.data.TimeSeriesX import TimeSeriesX


def run_lcf(events, eeg_dict, ephys_dir, method='fastica', highpass_freq=1, reref=True, skip_breaks=True,
            exclude_bad_channels=False, iqr_thresh=3, lcf_winsize=.1, save_format='.h5'):
    """
    Runs localized component filtering (DelPozo-Banos & Weidemann, 2017) to clean artifacts from EEG data. Cleaned data
    is written to a new file in the ephys directory for the session. The pipeline is as follows, repeated for each
    EEG recording the session had:

    1) Drop EOG channels and (optionally) bad channels from the data.
    2) High-pass filter the data.
    3) Common average re-reference the data.
    4) Run ICA on the data. This can be done in one of two ways:
        A) Run a single ICA for the entire session, with no time points excluded from the fitting process.
        B) Fit a new ICA after each session break, while excluding the time points before the session, after the
        session, and during breaks. The final saved file will still contain all data points, but the actual ICA
        solutions will not be influenced by breaks.
    5) Remove artifacts using LCF, paired with the ICA solutions calculated in #4.
    6) Save a cleaned version of the EEG data to a .fif file.

    :param events: Events structure for the session.
    :param eeg_dict: Dictionary mapping the basename of each EEG recording for the session to an MNE raw object
        containing the data from that recording.
    :param ephys_dir: File path of the current_processed EEG directory for the session.
    :param method: String defining which ICA algorithm to use (fastica, infomax, extended-infomax, picard).
    :param highpass_freq: The frequency in Hz at which to high-pass filter the data prior to ICA (recommended >= .5)
    :param reref: If True, common average references the data prior to ICA. If False, no re-referencing is performed.
    :param skip_breaks: If True, excludes EEG samples from ICA calculation during breaks as well as before or after the
        end of the session. Furthermore, separate ICA solutions will be calculated for each "phase" of the session (new
        ICA starts after each break). If False, one ICA solution will be calculated for the entire session.
    :param exclude_bad_channels: If True, excludes bad channels during ICA and leaves them out of the cleaned data.
    :param iqr_thresh: The number of interquartile ranges above the 75th percentile or below the 25th percentile that a
        sample must be for LCF to mark it as artifactual.
    :param lcf_winsize: The width (in seconds) of the LCF dilator and transition windows.
    :param save_format: The file format to which the cleaned data will be saved. '.h5' uses PTSA to save a TimeSeries to
        an HDF5 file. '.fif' uses MNE to save a Raw object to a .fif file.
    :return: None
    """

    # Loop over all of the session's EEG recordings
    for basename in eeg_dict:

        # Select EEG data and events from current recording
        eeg = eeg_dict[basename]
        samp_rate = eeg.info['sfreq']
        evs = events[events.eegfile == os.path.join(ephys_dir, '%s.bdf' % basename)]

        ##########
        #
        # EEG Pre-Processing
        #
        ##########

        # Drop EOG channels
        eeg.pick_types(eeg=True, eog=False)

        # High-pass filter the data, since LCF will not work properly if the baseline voltage shifts
        eeg.filter(highpass_freq, None, fir_design='firwin')

        # Load bad channel info
        badchan_file = os.path.join(ephys_dir, '%s_bad_chan.txt' % basename)
        eeg.load_bad_channels(badchan_file)

        # Convert EEG data from float64 to float32 to save memory, since the original recording was only int16 or int24
        # eeg._data = eeg._data.astype(np.float32)

        # Rereference data using the common average reference
        if reref:
            eeg.set_eeg_reference(projection=False)

        # By default, mne excludes bad channels during ICA. If not intending to exclude bad chans, clear bad chan list.
        if not exclude_bad_channels:
            eeg.info['bads'] = []

        ##########
        #
        # ICA (Skip breaks)
        #
        ##########

        if skip_breaks:
            onsets = []
            offsets = []
            sess_start_in_recording = False
            # Mark all time points before and after the session for exclusion
            if evs[0].type == 'SESS_START':
                sess_start_in_recording = True
                onsets.append(0)
                offsets.append(evs[0].eegoffset)
            if evs[-1].type == 'SESS_END':
                onsets.append(evs[-1].eegoffset)
                offsets.append(eeg.n_times - 1)

            # Mark breaks for exclusion
            # Identify break start/stop times. PyEPL used REST_REWET events; UnityEPL uses BREAK_START/STOP.
            rest_rewet_idx = np.where(evs.type == 'REST_REWET')[0]
            break_start_idx = np.where(evs[:-1].type == 'BREAK_START')[0]
            break_stop_idx = np.where(evs[1:].type == 'BREAK_STOP')[0]

            # Handling for PyEPL studies (only break starts are logged)
            if len(rest_rewet_idx > 0):
                onsets = evs[rest_rewet_idx].eegoffset
                for i, idx in enumerate(rest_rewet_idx):
                    # If break is the final event in the current recording, set the offset as the final sample
                    # Otherwise, set the offset as 5 seconds before the first event following the break
                    if len(evs) == idx + 1:
                        o = eeg.n_times - 1
                    else:
                        o = evs[idx + 1].eegoffset - 5 * samp_rate
                    # Make sure that offsets cannot occur before onsets (happens if a break lasts less than 5 seconds)
                    if o < onsets[i]:
                        o = onsets[i] + 1
                    offsets.append(o)

            # Handling for UnityEPL studies (break starts and stops are both logged)
            elif len(break_start_idx) > 0:
                # If the recordings starts in the middle of a break, the first event will be a break stop.
                # In this case, the break onset is set as the start of the recording.
                if evs[0].type == 'BREAK_STOP':
                    onsets.append(0)
                    offsets.append(evs[0].eegoffset)
                # If the recording ends in the middle of a break, the last event will be a break start.
                # In this case, set the break offset as the last time point in the recording.
                if evs[-1].type == 'BREAK_START':
                    onsets.append(evs[-1].eegoffset)
                    offsets.append(eeg.n_times-1)
                # All other break starts and stops are contained fully within the recording
                for i, idx in enumerate(break_start_idx):
                    onsets.append(evs[idx].eegoffset)
                    offsets.append(evs[break_stop_idx[i]].eegoffset)

            # Annotate the EEG object with the timings of excluded periods (pre-session, post-session, & breaks)
            onsets = np.sort(onsets)
            offsets = np.sort(offsets)
            onset_times = eeg.times[onsets]
            offset_times = eeg.times[offsets]
            durations = offset_times - onset_times
            descriptions = ['bad_break' for _ in onsets]
            annotations = mne.Annotations(eeg.times[onsets], durations, descriptions)
            eeg.annotations = annotations

            # Fit a new ICA after each break. For example, a session with 2 breaks would have 3 parts:
            # start of recording -> end of break 1
            # after end of break 1 -> end of break 2
            # after end of break 2 -> end of recording
            eeg_list = []
            ica_list = []
            start = 0
            for i, stop in enumerate(offsets):
                # We only want to split the ICA after breaks, so skip over the offset corresponding to the session start
                if i == 0 and sess_start_in_recording:
                    continue
                # Copy the session data, then crop it down to one part of the session
                eeg_list.append(eeg.copy())
                eeg_list[-1].crop(eeg.times[start], eeg.times[stop])
                # Run ICA on the current part of the session
                ica_list.append(mne.preprocessing.ICA(method=method))
                ica_list[-1].fit(eeg_list[-1], reject_by_annotation=True)
                # Set start point of next ICA to immediately follow the end of the break
                start = eeg.times[stop + 1]

        ##########
        #
        # ICA (Include breaks)
        #
        ##########

        else:
            ica = mne.preprocessing.ICA(method=method)
            ica.fit(eeg)
            eeg_list = [eeg]
            ica_list = [ica]

        ##########
        #
        # LCF
        #
        ##########

        for i, ica in enumerate(ica_list):
            # Convert data to sources
            S = ica.get_sources(eeg_list[i])._data
            # Clean artifacts from sources using LCF
            cS = lcf(S, S, samp_rate, iqr_thresh=iqr_thresh, dilator_width=lcf_winsize, transition_width=lcf_winsize)
            # Reconstruct data from cleaned sources
            eeg_list[i]._data = reconstruct_signal(cS, ica_list[i])

        # Concatenate the cleaned pieces of the recording back together
        clean = mne.concatenate_raws(eeg_list)

        ##########
        #
        # Save data & Clean up variables
        #
        ##########
        if save_format == '.h5':
            # Save cleaned version of data to hdf as a TimeSeriesX object
            clean_eegfile = os.path.join(ephys_dir, '%s_clean.h5' % basename)
            TimeSeriesX(clean._data.astype(np.float32), dims=('channels', 'time'),
                        coords={'channels': clean.info['ch_names'], 'time': clean.data.times,
                                'samplerate': clean.data.info['sfreq']}).to_hdf(clean_eegfile)
        elif save_format == '.fif':
            # Save cleaned version of data to an MNE raw.fif file
            clean.save(os.path.join(ephys_dir, '%s_clean_raw.fif' % basename))

        del clean, eeg_list, ica_list, S, cS


def lcf(S, feat, sfreq, iqr_thresh=3, dilator_width=.1, transition_width=.1):

    dilator_width = int(dilator_width * sfreq)
    transition_width = int(transition_width * sfreq)

    ##########
    #
    # Classification
    #
    ##########

    # Find interquartile range of each component
    p75 = np.percentile(feat, 75, axis=1)
    p25 = np.percentile(feat, 25, axis=1)
    iqr = p75 - p25

    # Tune artifact thresholds for each component according to the IQR and the iqr_thresh parameter
    pos_thresh = p75 + iqr * iqr_thresh
    neg_thresh = p25 - iqr * iqr_thresh

    # Detect artifacts using the IQR threshold. Dilate the detected zones to account for the mixer transition equation
    ctrl_signal = np.zeros(feat.shape, dtype=float)
    dilator = np.ones(dilator_width)
    for i in range(ctrl_signal.shape[0]):
        ctrl_signal[i, :] = (feat[i, :] > pos_thresh[i]) | (feat[i, :] < neg_thresh[i])
        ctrl_signal[i, :] = np.convolve(ctrl_signal[i, :], dilator, 'same')
    del p75, p25, iqr, pos_thresh, neg_thresh, dilator

    # Binarize signal
    ctrl_signal = (ctrl_signal > 0).astype(float)

    ##########
    #
    # Mixing
    #
    ##########

    # Allocate normalized transition window
    trans_win = sp_signal.hann(transition_width, True)
    trans_win /= trans_win.sum()

    # Pad extremes of control signal
    pad_width = [tuple([0, 0])] * ctrl_signal.ndim
    pad_size = int(transition_width / 2 + 1)
    pad_width[1] = (pad_size, pad_size)
    ctrl_signal = np.pad(ctrl_signal, tuple(pad_width), mode='edge')
    del pad_width

    # Combine the transition window and the control signal to build a final transition-control signal, which can be applied to the components
    for i in range(ctrl_signal.shape[0]):
        ctrl_signal[i, :] = np.convolve(ctrl_signal[i, :], trans_win, 'same')
    del trans_win

    # Remove padding from transition-control signal
    rm_pad_slice = [slice(None)] * ctrl_signal.ndim
    rm_pad_slice[1] = slice(pad_size, -pad_size)
    ctrl_signal = ctrl_signal[rm_pad_slice]
    del rm_pad_slice, pad_size

    # Mix sources with control signal to get cleaned sources
    S_clean = S * (1 - ctrl_signal)

    return S_clean


def reconstruct_signal(sources, ica):
    # Mix sources to translate back into PCA components (PCA components x Time)
    data = np.dot(ica.mixing_matrix_, sources)

    # Mix PCA components to translate back into original EEG channels (Channels x Time)
    data = np.dot(np.linalg.inv(ica.pca_components_), data)

    # Invert transformations that MNE performs prior to PCA
    data += ica.pca_mean_[:, None]
    data *= ica.pre_whitener_

    return data