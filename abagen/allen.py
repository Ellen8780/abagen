# -*- coding: utf-8 -*-
"""
Functions for mapping AHBA microarray dataset to atlases and and parcellations
"""

from functools import reduce

import numpy as np
import pandas as pd

from . import correct, datasets, io, probes, samples, utils

import logging
logging.basicConfig(format='%(levelname)s: %(message)s', level=logging.INFO)
lgr = logging.getLogger('abagen')
lgr_levels = dict(zip(range(3), [40, 20, 10]))


def groupby_label(microarray, sample_labels, labels=None, metric='mean'):
    """
    Averages expression data in `microarray` over samples with same label

    Parameters
    ----------
    microarray : (S, G) pandas.DataFrame
        Microarray expression data, where `S` is samples and `G` is genes
    sample_labels : (S, 1) pandas.DataFrame
        Parcel labels for `S` samples, as returned by e.g., `label_samples()`
    labels : (L,) array_like, optional
        All possible labels for parcellation (to account for possibility that
        some parcels have NO expression data). Default: None
    metric : str or func, optional
        Mechanism by which to collapse across samples within a parcel. If a
        str, should be in ['mean', 'median']; if a function, should be able to
        accept an `N`-dimensional input and the `axis` keyword argument and
        return an `N-1`-dimensional output. Default: 'mean'

    Returns
    -------
    gene_by_label : (L, G) pandas.DataFrame
        Microarray expression data
    """

    # get combination function
    metric = utils.check_metric(metric)

    # get missing labels
    if labels is not None:
        missing = np.setdiff1d(labels, sample_labels)
        labels = pd.DataFrame(columns=microarray.columns,
                              index=pd.Series(missing, name='label'))

    gene_by_label = (microarray.merge(sample_labels,
                                      left_index=True,
                                      right_index=True)
                               .groupby('label')
                               .aggregate(metric)
                               .append(labels)
                               .sort_index()
                               .rename_axis('label'))

    # remove "zero" label (if it exists)
    if 0 in gene_by_label.index:
        gene_by_label = gene_by_label.drop([0], axis=0)

    return gene_by_label


def get_expression_data(atlas, atlas_info=None, *, exact=True,
                        tolerance=2, metric='mean', ibf_threshold=0.5,
                        probe_selection='diff_stability', lr_mirror=False,
                        sample_norm='srs', donor_norm='srs',
                        corrected_mni=True, reannotated=True,
                        return_counts=False, return_donors=False,
                        donors='all', data_dir=None, verbose=1, n_proc=1):
    """
    Assigns microarray expression data to ROIs defined in `atlas`

    This function aims to provide a workflow for generating pre-processed,
    microarray expression data from the Allen Human Brain Atlas ([A2]_) for
    abitrary `atlas` designations. First, some basic filtering of genetic
    probes is performed, including:

        1. Intensity-based filtering of microarray probes to remove probes that
           do not exceed a certain level of background noise (specified via the
           `ibf_threshold` parameter), and
        2. Selection of a single, representative probe (or collapsing across
           probes) for each gene, specified via the `probe_selection`
           parameter.

    Tissue samples are then matched to parcels in the defined `atlas` for each
    donor. If `atlas_info` is provided then this matching is constrained by
    both hemisphere and tissue class designation (e.g., cortical samples from
    the left hemisphere are only matched to ROIs in the left cortex,
    subcortical samples from the right hemisphere are only matched to ROIs in
    the left subcortex); see the `atlas_info` parameter description for more
    information.

    Matching of microarray samples to parcels in `atlas` is done via a multi-
    step process:

        1. Determine if the sample falls directly within a parcel,
        2. Check to see if there are nearby parcels by slowly expanding the
           search space to include nearby voxels, up to a specified distance
           (specified via the `tolerance` parameter),
        3. If there are multiple nearby parcels, the sample is assigned to the
           closest parcel, as determined by the parcel centroid.

    If at any step a sample can be assigned to a parcel the matching process is
    terminated. If multiple sample are assigned to the same parcel they are
    aggregated with the metric specified via the `metric` parameter. More
    control over the sample matching can be obtained by setting the `exact`
    parameter; see the parameter description for more information.

    Once all samples have been matched to parcels for all supplied donors, the
    microarray expression data are optionally normalized via the provided
    `sample_norm` and `donor_norm` functions before being combined within
    parcels and across donors via the supplied `metric.

    Parameters
    ----------
    atlas : niimg-like object
        A parcellation image in MNI space, where each parcel is identified by a
        unique integer ID
    atlas_info : str or pandas.DataFrame, optional
        Filepath to or pre-loaded dataframe containing information about
        `atlas`. Must have at least columns 'id', 'hemisphere', and 'structure'
        containing information mapping atlas IDs to hemisphere (i.e, "L", "R")
        and broad structural class (i.e., "cortex", "subcortex", "cerebellum").
        Default: None
    exact : bool, optional
        Whether to use exact matching of donor tissue samples to parcels in
        `atlas`. If True, this function will match tissue samples to parcels
        within `threshold` mm of the sample; any samples that are beyond
        `threshold` mm of a parcel will be discarded. This may result in some
        parcels having no assigned sample / expression data. If False, the
        default matching procedure will be performed and followed by a check
        for parcels with no assigned samples; any such parcels will be matched
        to the nearest sample (nearest defined as the sample with the closest
        Euclidean distance to the parcel centroid). Default: True
    tolerance : int, optional
        Distance (in mm) that a sample must be from a parcel for it to be
        matched to that parcel. This is only considered if the sample is not
        directly within a parcel. Default: 2
    metric : {'mean', 'median'} or callable, optional
        Mechanism by which to reduce donor-level expression data into a single
        dataframe. If a callable, should be able to accept an `N`-dimensional
        input and the `axis` keyword argument and return an `N-1`-dimensional
        output. Default: 'mean'
    ibf_threshold : [0, 1] float, optional
        Threshold for intensity-based filtering specifying. This number should
        specify the ratio of samples, across all supplied donors, for which a
        probe must have signal above background noise in order to be retained.
        Default: 0.5
    probe_selection : str, optional
        Selection method for subsetting (or collapsing across) probes that
        index the same gene. Must be one of 'average', 'max_intensity',
        'max_variance', 'pc_loading', 'corr_variance', 'corr_intensity', or
        'diff_stability'; see Notes for more information. Default:
        'diff_stability'
    lr_mirror : bool, optional
        Whether to mirror microarray expression samples across hemispheres to
        increase spatial coverage. This will duplicate samples across both
        hemispheres (i.e., L->R and R->L), approximately doubling the number of
        available samples. Default: False
    sample_norm : {'rs', 'srs', 'minmax', 'center', 'zscore', None}, optional
        Method by which to normalize microarray expression values for each
        sample. Expression values are normalized separately for each sample for
        each donor across all genes; see Notes for more information on
        different methods. If None is specified then no normalization is
        performed. Default: 'srs'
    donor_norm : {'rs', 'srs', 'minmax', 'center', 'zscore', None}, optional
        Method by which to normalize microarray expression values for each
        donor. Expression values are normalized separately for each gene for
        each donor across all regions in `atlas`; see Notes for more
        information on different methods. If None is specified then no
        normalization is performed. Default: 'srs'
    corrected_mni : bool, optional
        Whether to use the "corrected" MNI coordinates shipped with the
        `alleninf` package instead of the coordinates provided with the AHBA
        data when matching tissue samples to anatomical regions. Default: True
    reannotated : bool, optional
        Whether to use reannotated probe information provided by [A1]_ instead
        of the default probe information from the AHBA dataset. Using
        reannotated information will discard probes that could not be reliably
        matched to genes. Default: True
    return_counts : bool, optional
        Whether to return how many samples were assigned to each parcel in
        `atlas` for each donor. Default: False
    return_donors : bool, optional
        Whether to return donor-level expression arrays instead of aggregating
        expression across donors with provided `metric`. Default: False
    donors : list, optional
        List of donors to use as sources of expression data. Can be either
        donor numbers or UID. If not specified will use all available donors.
        Default: 'all'
    data_dir : str, optional
        Directory where expression data should be downloaded (if it does not
        already exist) / loaded. If not specified will use the current
        directory. Default: None
    verbose : int, optional
        Specifies verbosity of status messages to display during workflow.
        Higher numbers increase verbosity of messages while zero suppresses all
        messages. Default: 1
    n_proc : int, optional
        Number of processors to use to download AHBA data. Can parallelize up
        to six times. Default: 1

    Returns
    -------
    expression : (R, G) pandas.DataFrame
        Microarray expression for `R` regions in `atlas` for `G` genes,
        aggregated across donors, where the index corresponds to the unique
        integer IDs of `atlas` and the columns are gene names. If
        ``return_donors`` is set to ``True`` then this is a list of (R, G)
        dataframes, one for each donor.
    counts : (R, D) pandas.DataFrame
        Number of samples assigned to each of `R` regions in `atlas` for each
        of `D` donors (if multiple donors were specified); only returned if
        ``return_counts`` is set to ``True``.

    Notes
    -----
    The following methods can be used for collapsing across probes when
    multiple probes are available for the same gene.

        1. ``probe_selection='average'``

        Takes the average of expression data across all probes indexing the
        same gene. Providing 'mean' as the input method will return the same
        thing.

        2. ``probe_selection='max_intensity'``

        Selects the probe with the maximum average expression across samples
        from all donors.

        3. ``probe_selection='max_variance'``

        Selects the probe with the maximum variance in expression across
        samples from all donors.

        4. ``probe_selection='pc_loading'``

        Selects the probe with the maximum loading along the first principal
        component of a decomposition performed across samples from all donors.

        5. ``probe_selection='corr_intensity'``

        Selects the probe with the maximum correlation to other probes from the
        same gene when >2 probes exist; otherwise, uses the same procedure as
        `max_intensity`.

        6. ``probe_selection='corr_variance'``

        Selects the probe with the maximum correlation to other probes from the
        same gene when >2 probes exist; otherwise, uses the same procedure as
        `max_varance`.

        7. ``probe_selection='diff_stability'``

        Selects the probe with the most consistent pattern of regional
        variation across donors (i.e., the highest average correlation across
        brain regions between all pairs of donors).

    The following methods can be used for normalizing microarray expression
    values for each donor prior to aggregating:

        1. ``norm=='rs'``

        Uses a robust sigmoid function as in [A3]_ to normalize values

        2. ``norm='srs'``

        Same as 'rs' but scales output to the unit normal (i.e., range 0-1)

        3. ``norm='minmax'``

        Scales data in each column to the unit normal (i.e., range 0-1)

        4. ``norm='center'``

        Removes the mean of expression values

        5. ``norm='zscore'``

        Applies a basic z-score (subtract mean, divide by standard deviation);
        uses degrees of freedom equal to one for standard deviation

    References
    ----------
    .. [A1] Arnatkevic̆iūtė, A., Fulcher, B. D., & Fornito, A. (2019). A
       practical guide to linking brain-wide gene expression and neuroimaging
       data. NeuroImage, 189, 353-367.
    .. [A2] Hawrylycz, M.J. et al. (2012) An anatomically comprehensive atlas
       of the adult human transcriptome. Nature, 489, 391-399.
    .. [A3] Fulcher, B. D., & Fornito, A. (2016). A transcriptional signature
       of hub connectivity in the mouse connectome. Proceedings of the National
       Academy of Sciences, 113(5), 1435-1440.
    """

    # set logging verbosity level
    lgr.setLevel(lgr_levels.get(int(verbose), 1))

    # load atlas_info, if provided
    atlas = utils.check_img(atlas)
    if atlas_info is not None:
        atlas_info = utils.check_atlas_info(atlas, atlas_info)

    # get combination functions
    metric = utils.check_metric(metric)

    # check probe_selection input
    if probe_selection not in probes.SELECTION_METHODS:
        raise ValueError('Provided probe_selection method is invalid, must be '
                         'one of {}. Received value: \'{}\''
                         .format(list(probes.SELECTION_METHODS),
                                 probe_selection))

    # fetch files (downloading if necessary) and unpack to variables
    files = datasets.fetch_microarray(data_dir=data_dir, donors=donors,
                                      verbose=verbose, n_proc=n_proc)
    microarray = files['microarray']
    annotation = files['annotation']
    pacall = files['pacall']
    ontology = files['ontology']
    probe_info = files['probes'][0]

    if probe_selection == 'diff_stability' and len(microarray) == 1:
        raise ValueError('Cannot use diff_stability for probe_selection with '
                         'only one donor. Please specify a different probe_'
                         'selection method or use more donors.')

    # get some info on labels in `atlas_img`
    all_labels = utils.get_unique_labels(atlas)
    if not exact:
        lgr.info('Pre-calculating centroids for {} regions in provided atlas'
                 .format(len(all_labels)))
        centroids = utils.get_centroids(atlas, labels=all_labels)

    # update the annotation "files"
    # this handles updating the MNI coordinates, dropping mistmatched samples
    # (where MNI coordinates don't match ontology), and mirror samples across
    # hemisphere, as applicable.
    for n, (annot, ont) in enumerate(zip(annotation, ontology)):
        annotation[n] = samples.update_samples(annot, ont, lr_mirror=lr_mirror,
                                               corrected_mni=corrected_mni)

    # get dataframe of probe information (reannotated or otherwise)
    probe_info = io.read_probes(probe_info, copy=True)
    if reannotated:
        lgr.info('Reannotating probes with information from Arnatkevic̆iūtė '
                 'et al., 2019, NeuroImage')
        probe_info = probes.reannotate_probes(probe_info)

    # intensity-based filtering of probes
    probe_info = probes.filter_probes(pacall, annotation, probe_info,
                                      threshold=ibf_threshold)
    lgr.info('{} probes survive intensity-based filtering with threshold of {}'
             .format(len(probe_info), ibf_threshold))

    # get probe-reduced microarray expression data for all donors based on
    # selection method; this will be a list of gene x sample dataframes (one
    # for each donor)
    lgr.info('Reducing probes indexing same gene with provided method: "{}"'
             .format(probe_selection))
    microarray = probes.collapse_probes(microarray, annotation,
                                        probe_info, method=probe_selection,
                                        inplace=True)
    lgr.info('{} genes remain after probe filtering and selection'
             .format(microarray[0].shape[-1]))

    expression, missing = [], []
    counts = pd.DataFrame(np.zeros((len(all_labels) + 1, len(microarray))),
                          index=np.append([0], all_labels))
    for subj in range(len(microarray)):
        # assign samples to regions
        labels = samples.label_samples(annotation[subj], atlas,
                                       atlas_info=atlas_info,
                                       tolerance=tolerance)

        if exact:  # remove all samples not assigned a label before norming
            nz = np.asarray(labels != 0).squeeze()
            microarray[subj] = microarray[subj].loc[nz]
            labels = labels[nz]

        if sample_norm is not None:
            if subj == 0:  # only provide norm log once
                lgr.info('Normalizing expression across genes with function: '
                         '{}'.format(sample_norm))
            microarray[subj] = correct.normalize_expression(microarray[subj].T,
                                                            norm=sample_norm).T
        if donor_norm is not None:
            if subj == 0:  # only provide norm log once
                lgr.info('Normalizing expression across sample with function: '
                         '{}'.format(donor_norm))
            microarray[subj] = correct.normalize_expression(microarray[subj],
                                                            norm=donor_norm)

        # aggregate normalized samples within regions
        expression += [groupby_label(microarray[subj], labels,
                                     all_labels, metric=metric)]
        lgr.info('{:>3} / {} samples matched to regions for donor #{}'
                 .format(np.sum(np.asarray(labels) != 0),
                         len(annotation[subj]),
                         datasets.WELL_KNOWN_IDS.value_set('subj')[subj]))

        # get counts of samples collapsed into each ROI
        labs, num = np.unique(labels, return_counts=True)
        counts.loc[labs, subj] = num

        # if we don't want to do exact matching then cache which parcels are
        # missing data and the expression data for the closest sample to that
        # parcel; we'll use this once we've iterated through all donors
        if not exact:
            cols = ['mni_x', 'mni_y', 'mni_z']
            coords = utils.xyz_to_ijk(annotation[subj][cols], atlas.affine)
            empty = np.logical_not(np.in1d(all_labels, labs))
            idx, dist = utils.closest_centroid(coords, centroids[empty],
                                               return_dist=True)
            idx = microarray[subj].loc[annotation[subj].iloc[idx].index]
            empty = all_labels[empty]
            idx.index = pd.Series(empty, name='label')
            missing += [(idx, dict(zip(empty, np.diag(dist))))]

    # check for missing ROIs and fill in, as needed
    if not exact:
        # find labels that are missing across all donors
        empty = reduce(set.intersection, [set(f.index) for f, d in missing])
        lgr.info('Matching {} regions with no data to nearest samples'
                 .format(len(empty)))
        for roi in empty:
            # find donor with sample closest to centroid of empty parcel
            ind = np.argmin([d.get(roi) for f, d in missing])
            donor = datasets.WELL_KNOWN_IDS.value_set("subj")[ind]
            lgr.debug('Assigning sample from donor {} to region id #{}'
                      .format(donor, roi))
            # assign expression data from that sample and add to count
            expression[ind].loc[roi] = missing[ind][0].loc[roi]
            counts.loc[roi, ind] += 1

    # aggregate across donors if individual donor dataframes not requested
    if not return_donors:
        lgr.info('Aggregating donor expression data with function: "{}"'
                 .format(metric))
        expression = pd.concat(expression).groupby('label').aggregate(metric)
        # some genes may have been poorly normalized; remove these
        drop = expression.dropna(axis=0, how='all').isna().any(axis=0)
        expression = expression.drop(drop[drop].index, axis=1)

    # drop the "zero" label from the counts dataframe (this is background)
    if return_counts:
        return expression, counts.iloc[1:]

    return expression
