import numpy as np
import itertools

from time import time

from .utils.partition import masters_cut

from .information_measures.discrete import mutinf_discrete_calc_parallel
from .information_measures.discrete import ur_calc_parallel
from .information_measures.discrete import mutinf_discrete_calc_gpu

from .information_measures.knn import mutinf_knn_calc

from .utils.permutation import create_permutation_matrix
from .utils.pvalue import solo_pvalue, unbiased_pvalue, p_median_calc


def univariate_discrete(x_bin_vars, y_bin_vars, n_reps, criterion='mi'):
    # Confirm that valid criterion is present
    assert criterion in ['mi','ur'], \
        "'criterion' must be either 'mi' or 'ur'."
    # create the marginal and bin arrays
    target = None
    if isinstance(x_bin_vars['bins'], np.ndarray):
        nbins_x = x_bin_vars['n_bins']
        nbins_y = y_bin_vars['n_bins']
        # Test to make sure n_reps value corresponds accordingly
        # to the 'bins_permuted' matrix
        assert (n_reps + 1) == y_bin_vars['bins_permuted'].shape[0], \
            "n_reps value differs from what is expected in 'bins_permuted'."
        target = 'cpu'
    else:
        # run on gpu
        nbins_x = x_bin_vars['n_bins'].copy_to_host()
        nbins_y = y_bin_vars['n_bins'].copy_to_host()
        target = 'gpu'

    # Since nbins_x and nbins_y are two-dimensional need to find
    # the max number of bins for each variable being evaluated and
    # create marginal/grid dimensions to these values

    ncols = nbins_x.shape[0]

    nbins_x = np.max(nbins_x)
    nbins_y = np.max(nbins_y)

    marginal_x = np.zeros((ncols, n_reps+1, nbins_x), np.int32)
    marginal_y = np.zeros((ncols, n_reps+1, nbins_y), np.int32)
    grid = np.zeros((ncols, n_reps+1, nbins_x * nbins_y), np.int32)

    criterion_matrix = np.zeros((ncols, n_reps+1), np.float32)

    if target == 'cpu':
        if criterion == 'mi':
            mutinf_discrete_calc_parallel(criterion_matrix,
                                          x_bin_vars['bins'],
                                          y_bin_vars['bins_permuted'],
                                          marginal_x, marginal_y, grid)
        else:
            # criterion == 'ur'
            ur_calc_parallel(criterion_matrix,
                            x_bin_vars['bins'], y_bin_vars['bins_permuted'],
                            marginal_x, marginal_y, grid)
    else:
        # target == 'gpu'
        marginal_x_gpu = cuda.to_device(marginal_x)
        marginal_y_gpu = cuda.to_device(marginal_y)
        grid_gpu = cuda.to_device(grid)
        criterion_gpu = cuda.to_device(criterion_matrix)
        configured = mutinf_discrete_calc_gpu.forall(criterion_gpu.size)
        configured(criterion_gpu.size, criterion_gpu,
                   x_bin_vars['bins'], y_bin_vars['bins_permuted'],
                   marginal_x_gpu, marginal_y_gpu, grid_gpu)
        criterion_matrix = criterion_gpu.copy_to_host()
        # Free up memory on GPU
        del marginal_x_gpu
        del marginal_y_gpu
        del grid_gpu

    return criterion_matrix


def univariate_info_calc(X, y,
                         method='discrete',
                         measure='mi',
                         target_is_discrete=False,
                         column_type_mask=None,
                         n_bins_x=5, n_bins_y=5,
                         n_reps=100,
                         target='cpu',
                         verbose=False):

    # Create the enpty information matrix that is going to house the
    # information values of all variables and permutation reps. The
    # calculated information values will then be entered in at their
    # appropriate indices.
    # This object is necessary regardless of method used
    information_matrix = np.zeros((X.shape[1], n_reps+1), np.float32)

    if method == 'discrete':
        # If 'y' is already a discrete variable, then the variable does
        # not have to be discretized using 'masters_cut' and the
        # necessary dictionary can be easily created
        if not target_is_discrete:
            y_bin_vars = masters_cut(y, nbins=n_bins_y, target=target)
        else:
            # Need to make sure that 'y' values are ultimately cast as
            # integers since the values will be referenced in the marginals
            # and grid population parts.
            y_bin_vars = {}
            n_bins = len(np.unique(y))
            y_bin_vars['n_bins'] = np.reshape(n_bins, (1,1))
            y_bin_vars['bins'] = np.reshape(y.astype(np.int32), (1, y.shape[0]))

        y_bin_vars['bins_permuted'] = \
            create_permutation_matrix(y_bin_vars, n_reps=n_reps)

        # Identify the different column types and iterate through them
        column_types = np.unique(column_type_mask)
        for col_type in column_types:
            if verbose:
                print("Column type: " + col_type)
            col_type_idx = np.where(column_type_mask == col_type)[0]
            if col_type == 'numeric':
                t0 = time()
                X_bin_vars = masters_cut(X[:,col_type_idx],
                                         nbins=n_bins_x, target=target)
                if verbose:
                    t1 = time()
                    print("   Numeric masters cut time: {0:.3g} sec".format(t1 - t0))
            elif col_type == 'discrete':
                t2 = time()
                X_bin_vars = masters_cut(X[:,col_type_idx],
                                         nbins=None, target=target)
                if verbose:
                    t3 = time()
                    print("   Discrete masters cut time: {0:.3g} sec".format(t3 - t2))
            else:
                pass
            t4 = time()
            information_matrix[col_type_idx] = \
                univariate_discrete(X_bin_vars, y_bin_vars,
                                    n_reps=n_reps, criterion=measure)
            if verbose:
                t5 = time()
                print("   Information calculation time: {0:.3g} sec".format(t5 - t4))
    elif method == 'knn':
        # set the numeric value for y column type and create the
        # permutation matrix
        y_bin_vars = {}
        if target_is_discrete:
            y_type = 1
            y_bin_vars['bins'] = np.reshape(y.astype(np.int64), (1, y.shape[0]))
        else:
            # target is numeric
            y_type = 0
            y_bin_vars['bins'] = np.reshape(y, (1, y.shape[0]))
        # create the permutation matrix
        y_matrix = create_permutation_matrix(y_bin_vars, n_reps=n_reps)
        # convert column_type_mask to numbers for numba integration
        type_dict = {'discrete': 1, 'numeric': 0}
        x_types = np.array([type_dict[i] for i in column_type_mask], dtype=np.int8)
        # In order for the tree algorithms to work correctly, the X DataFrame's needs
        # to be transposed into it's values (numpy ndarray) and it needs to be made
        # into a contiguous array.
        X = np.ascontiguousarray(X.transpose())
        # perform the knn mutual information calculations
        information_matrix = mutinf_knn_calc(X, y_matrix, x_types, y_type)
    else:
        # 'adaptive' method not yet implemented
        raise ValueError

    return information_matrix


def univariate_pval_calc(information_matrix):

    info_actual = information_matrix[:,0]

    if information_matrix.shape[1] > 1:
        solo_pval = solo_pvalue(information_matrix)
        unbiased_pval = unbiased_pvalue(information_matrix)
        # Create new 2d numpy matrix that contains the information
        # value and all the associated p-values
        information_matrix = \
            np.column_stack((info_actual, solo_pval, unbiased_pval))
    else:
        information_matrix = np.reshape(info_actual,(info_acutal.shape[0],1))

    return information_matrix


def univariate_cscv(x, y, n_folds, **kwargs):

    assert n_folds % 2 == 0, "n_folds value must be a multiple of 2!"

    folds = [x for x in range(n_folds)]
    folds_set = set(folds)

    initial_folds_combos = itertools.combinations(folds, int(n_folds/2))
    in_sample = [set(i) for i in initial_folds_combos]
    out_of_sample = [folds_set - x for x in in_sample]

    # Next, create a dictionary of the folds using in_sample as the base
    fold_dict = dict(zip(range(len(in_sample)), in_sample))
    # Get a list of all the keys from fold_dict
    in_sample_key = [i for i in range(len(in_sample))]
    # Finally, get the keys from fold_dict for the out_of_sample values
    key_list = list(fold_dict.keys())
    val_list = list(fold_dict.values())
    out_of_sample_key = [key_list[val_list.index(j)] for j in out_of_sample]

    # Create a list of 'fold_tuples' where first value is index to the
    # in-sample folds and the second value is a list to the
    # out-of-sample folds
    in_out_sample_key_zip = zip(in_sample_key, out_of_sample_key)
    fold_tuples = [(i, o) for (i,o) in in_out_sample_key_zip]

    # Create a list of the rows/oberservations evenly divided into
    # n_folds. Each fold should roughly have the same number of
    # observations
    idxs = np.arange(x.shape[0], dtype=np.uint32)
    fold_index_list = np.array_split(idxs, n_folds)

    # The actual information calculation for each predictor variable over
    # each fold combination will take an extremely long amount of time unless
    # the operation is parallelized.
    #
    # In order to efficiently parallelize, create one large list of tuples
    # First element of tuple will be name of predictor variable
    # Second element of tuple will be another tuple with the number of the fold
    # combination along with the indices for that particular fold combo
    #
    # This list of tuples will then be passed into a parallel-enabled numba
    # function that will iterate through each combination.

    # Create list of tuples with nested 'for' loop
    parallel_tuple_list = []
    # Iterate through every set of folds present in fold_dict
    # The indices for all the folds in the set need to be combined in order
    # for mutual information to be calculated using only those observations
    combo_idx_count = 0
    for fold_combo, set_value in fold_dict.items():
        # The combo_idx array will hold all the index values that will
        # be evaluated in the specific fold_combo
        combo_idx = None
        for fold in list(set_value):
            fold_idx = fold_index_list[fold]
            if combo_idx is None:
                combo_idx = fold_idx
            else:
                combo_idx = np.concatenate([combo_idx, fold_idx])
            # sort the combo_idx array
            combo_idx = np.sort(combo_idx)
        # In order to better parallelize the operations, create a tuple
        # with combo_idx and the count
        combo_idx_tuple = (combo_idx_count, combo_idx)
        # Add this tuple to the list that will be iterated over
        parallel_tuple_list += [combo_idx_tuple]
        # Increment the combo_idx_count by 1
        combo_idx_count += 1

    # Initialize the cscv matrix that will be used to store MI values
    # 'mi_matrix_cscv':
    #   rows = predictor variables
    #   columns = fold_combo_number
    #   cell = information value for predictor variable in given fold_combo
    info_matrix_cscv = \
        np.zeros((x.shape[1], combo_idx_count), dtype=np.float32)

    # Iterate through the parallel_tuple list and calculate the information
    # measure for each one of the independent variables for each set of the
    # fold indices. Unfortunately, won't be able to parallelize this operation
    # entirely due to the dynamic observations across multiple folds
    for (i, idx) in parallel_tuple_list:
        info_matrix = \
            univariate_info_calc(x[idx,:], y[idx], n_reps=0, **kwargs)
        # result should be single column (since n_reps == 0)
        # reshape to array and assign to column 'i' in info_matrix_cscv
        info_matrix = np.reshape(info_matrix, (info_matrix.shape[0],))
        info_matrix_cscv[:,i] = info_matrix

    return info_matrix_cscv, fold_tuples


def screen_univariate(X, y,
                      method='discrete',
                      measure='mi',
                      target_is_discrete=False,
                      column_type_mask=None,
                      n_bins_x=5, n_bins_y=5,
                      n_reps=100,
                      cscv_folds=None,
                      target='cpu',
                      verbose=False):

    kwargs = {'method': method, 'measure': measure, \
              'target_is_discrete': target_is_discrete, \
              'column_type_mask': column_type_mask, \
              'n_bins_x': n_bins_x, 'n_bins_y': n_bins_y, \
              'target': target, 'verbose': verbose }

    # Regardless of whether cscv calculation is desired,
    # execute the screen_univariate calculations with
    # solo and unbiased p_values
    #
    # If column_type_mask is None, then assume all columns are of
    # numeric type and that masters_cut needs to be run on all columns
    info_matrix = univariate_info_calc(X, y, n_reps=n_reps, **kwargs)
    info_matrix = univariate_pval_calc(info_matrix)

    # If CSCV is desired, then there will be a value in 'folds' parameter
    if cscv_folds is not None:
        assert isinstance(cscv_folds, int) and cscv_folds >= 2, \
            "'cscv_folds' param must be an even integer value of 2 or greater!"
        # Execute the univariate_cscv calculations
        # the resulting matrix will be:
        # rows - independent variables
        # columns - fold iteration
        cscv_matrix, fold_tuples = \
            univariate_cscv(X, y, n_folds=cscv_folds, **kwargs)
        # once the cscv_matrix is calculated, will need to calculate
        # the P(>=median) value for each one of the variables according to
        # each fold
        # calculate the median information value across each fold
        # resulting array will be number of columns in cscv_matrix
        cscv_fold_medians = np.median(cscv_matrix, axis=0)
        # create the array to store the p(>=median) values for each
        # independent variable (number of columns in x)
        p_median = np.zeros(X.shape[1], dtype=np.float32)
        # execute the p_median calculation
        p_median_calc(cscv_matrix, fold_tuples, cscv_fold_medians, p_median)
        # Add p(>=median) values to end of info_matrix
        p_median = np.reshape(p_median, (p_median.shape[0], 1))
        info_matrix = np.append(info_matrix, p_median, axis=1)

    return info_matrix
