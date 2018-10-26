import sys
import os
import pandas
import re
import pickle
import warnings
import numpy as np
import xgboost
import scipy
import sklearn
from asteval import Interpreter, make_symbol_table
from sklearn import (cluster, decomposition, ensemble, feature_extraction, feature_selection,
                    gaussian_process, kernel_approximation, metrics,
                    model_selection, naive_bayes, neighbors, pipeline, preprocessing,
                    svm, linear_model, tree, discriminant_analysis)

N_JOBS = int(os.environ.get('GALAXY_SLOTS', 1))


class SafePickler(pickle.Unpickler):
    """
    Used to safely deserialize scikit-learn model objects serialized by cPickle.dump
    Usage:
        eg.: SafePickler.load(pickled_file_object)
    """
    def find_class(self, module, name):

        bad_names = ('and', 'as', 'assert', 'break', 'class', 'continue',
                    'def', 'del', 'elif', 'else', 'except', 'exec',
                    'finally', 'for', 'from', 'global', 'if', 'import',
                    'in', 'is', 'lambda', 'not', 'or', 'pass', 'print',
                    'raise', 'return', 'try', 'system', 'while', 'with',
                    'True', 'False', 'None', 'eval', 'execfile', '__import__',
                    '__package__', '__subclasses__', '__bases__', '__globals__',
                    '__code__', '__closure__', '__func__', '__self__', '__module__',
                    '__dict__', '__class__', '__call__', '__get__',
                    '__getattribute__', '__subclasshook__', '__new__',
                    '__init__', 'func_globals', 'func_code', 'func_closure',
                    'im_class', 'im_func', 'im_self', 'gi_code', 'gi_frame',
                    '__asteval__', 'f_locals', '__mro__')
        good_names = ['copy_reg._reconstructor', '__builtin__.object']

        if re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', name):
            fullname = module + '.' + name
            if (fullname in good_names)\
                or  (   (   module.startswith('sklearn.')
                            or module.startswith('xgboost.')
                            or module.startswith('skrebate.')
                            or module.startswith('imblearn')
                            or module.startswith('numpy.')
                            or module == 'numpy'
                        )
                        and (name not in bad_names)
                    ):
                # TODO: replace with a whitelist checker
                if fullname not in sk_whitelist['SK_NAMES'] + sk_whitelist['SKR_NAMES'] + sk_whitelist['XGB_NAMES'] + sk_whitelist['NUMPY_NAMES'] + sk_whitelist['IMBLEARN_NAMES'] + good_names:
                    print("Warning: global %s is not in pickler whitelist yet and will loss support soon. Contact tool author or leave a message at github.com" % fullname)
                mod = sys.modules[module]
                return getattr(mod, name)

        raise pickle.UnpicklingError("global '%s' is forbidden" % fullname)


def load_model(file):
    return SafePickler(file).load()


def read_columns(f, c=None, c_option='by_index_number', return_df=False, **args):
    data = pandas.read_csv(f, **args)
    if c_option == 'by_index_number':
        cols = list(map(lambda x: x - 1, c))
        data = data.iloc[:, cols]
    if c_option == 'all_but_by_index_number':
        cols = list(map(lambda x: x - 1, c))
        data.drop(data.columns[cols], axis=1, inplace=True)
    if c_option == 'by_header_name':
        cols = [e.strip() for e in c.split(',')]
        data = data[cols]
    if c_option == 'all_but_by_header_name':
        cols = [e.strip() for e in c.split(',')]
        data.drop(cols, axis=1, inplace=True)
    y = data.values
    if return_df:
        return y, data
    else:
        return y


## generate an instance for one of sklearn.feature_selection classes
def feature_selector(inputs):
    selector = inputs["selected_algorithm"]
    selector = getattr(sklearn.feature_selection, selector)
    options = inputs["options"]

    if inputs['selected_algorithm'] == 'SelectFromModel':
        if not options['threshold'] or options['threshold'] == 'None':
            options['threshold'] = None
        else:
            try:
                options['threshold'] = float(options['threshold'])
            except ValueError:
                pass
        if inputs['model_inputter']['input_mode'] == 'prefitted':
            model_file = inputs['model_inputter']['fitted_estimator']
            with open(model_file, 'rb') as model_handler:
                fitted_estimator = load_model(model_handler)
            new_selector = selector(fitted_estimator, prefit=True, **options)
        else:
            estimator_json = inputs['model_inputter']["estimator_selector"]
            estimator = get_estimator(estimator_json)
            new_selector = selector(estimator, **options)

    elif inputs['selected_algorithm'] == 'RFE':
        estimator = get_estimator(inputs["estimator_selector"])
        new_selector = selector(estimator, **options)

    elif inputs['selected_algorithm'] == 'RFECV':
        options['scoring'] = get_scoring(options['scoring'])
        options['n_jobs'] = N_JOBS
        splitter, groups = get_cv(options.pop('cv_selector'))
        # TODO support group cv splitters
        options['cv'] = splitter
        estimator = get_estimator(inputs["estimator_selector"])
        new_selector = selector(estimator, **options)

    elif inputs['selected_algorithm'] == "VarianceThreshold":
        new_selector = selector(**options)

    else:
        score_func = inputs["score_func"]
        score_func = getattr(sklearn.feature_selection, score_func)
        new_selector = selector(score_func, **options)

    return new_selector


def get_X_y(params, file1, file2):
    input_type = params["selected_tasks"]["selected_algorithms"]["input_options"]["selected_input"]
    if input_type == "tabular":
        header = 'infer' if params["selected_tasks"]["selected_algorithms"]["input_options"]["header1"] else None
        column_option = params["selected_tasks"]["selected_algorithms"]["input_options"]["column_selector_options_1"]["selected_column_selector_option"]
        if column_option in ["by_index_number", "all_but_by_index_number", "by_header_name", "all_but_by_header_name"]:
            c = params["selected_tasks"]["selected_algorithms"]["input_options"]["column_selector_options_1"]["col1"]
        else:
            c = None
        X = read_columns(
            file1,
            c=c,
            c_option=column_option,
            sep='\t',
            header=header,
            parse_dates=True
        )
    else:
        X = mmread(file1)

    header = 'infer' if params["selected_tasks"]["selected_algorithms"]["input_options"]["header2"] else None
    column_option = params["selected_tasks"]["selected_algorithms"]["input_options"]["column_selector_options_2"]["selected_column_selector_option2"]
    if column_option in ["by_index_number", "all_but_by_index_number", "by_header_name", "all_but_by_header_name"]:
        c = params["selected_tasks"]["selected_algorithms"]["input_options"]["column_selector_options_2"]["col2"]
    else:
        c = None
    y = read_columns(
        file2,
        c=c,
        c_option=column_option,
        sep='\t',
        header=header,
        parse_dates=True
    )
    y = y.ravel()
    return X, y


class SafeEval(Interpreter):

    def __init__(self, load_scipy=False, load_numpy=False, load_estimators=False):

        # File opening and other unneeded functions could be dropped
        unwanted = ['open', 'type', 'dir', 'id', 'str', 'repr']

        # Allowed symbol table. Add more if needed.
        new_syms = {
            'np_arange': getattr(np, 'arange'),
            'ensemble_ExtraTreesClassifier': getattr(ensemble, 'ExtraTreesClassifier')
        }

        syms = make_symbol_table(use_numpy=False, **new_syms)

        if load_scipy:
            scipy_distributions = scipy.stats.distributions.__dict__
            for k, v in scipy_distributions.items():
                if isinstance(v, (scipy.stats.rv_continuous, scipy.stats.rv_discrete)):
                    syms['scipy_stats_' + k] = v

        if load_numpy:
            from_numpy_random = ['beta', 'binomial', 'bytes', 'chisquare', 'choice', 'dirichlet', 'division',
                                'exponential', 'f', 'gamma', 'geometric', 'gumbel', 'hypergeometric',
                                'laplace', 'logistic', 'lognormal', 'logseries', 'mtrand', 'multinomial',
                                'multivariate_normal', 'negative_binomial', 'noncentral_chisquare', 'noncentral_f',
                                'normal', 'pareto', 'permutation', 'poisson', 'power', 'rand', 'randint',
                                'randn', 'random', 'random_integers', 'random_sample', 'ranf', 'rayleigh',
                                'sample', 'seed', 'set_state', 'shuffle', 'standard_cauchy', 'standard_exponential',
                                'standard_gamma', 'standard_normal', 'standard_t', 'triangular', 'uniform',
                                'vonmises', 'wald', 'weibull', 'zipf']
            for f in from_numpy_random:
                syms['np_random_' + f] = getattr(np.random, f)

        if load_estimators:
            estimator_table = {
                'sklearn_svm' : getattr(sklearn, 'svm'),
                'sklearn_tree' : getattr(sklearn, 'tree'),
                'sklearn_ensemble' : getattr(sklearn, 'ensemble'),
                'sklearn_neighbors' : getattr(sklearn, 'neighbors'),
                'sklearn_naive_bayes' : getattr(sklearn, 'naive_bayes'),
                'sklearn_linear_model' : getattr(sklearn, 'linear_model'),
                'sklearn_cluster' : getattr(sklearn, 'cluster'),
                'sklearn_decomposition' : getattr(sklearn, 'decomposition'),
                'sklearn_preprocessing' : getattr(sklearn, 'preprocessing'),
                'sklearn_feature_selection' : getattr(sklearn, 'feature_selection'),
                'sklearn_kernel_approximation' : getattr(sklearn, 'kernel_approximation'),
                'skrebate_ReliefF': getattr(skrebate, 'ReliefF'),
                'skrebate_SURF': getattr(skrebate, 'SURF'),
                'skrebate_SURFstar': getattr(skrebate, 'SURFstar'),
                'skrebate_MultiSURF': getattr(skrebate, 'MultiSURF'),
                'skrebate_MultiSURFstar': getattr(skrebate, 'MultiSURFstar'),
                'skrebate_TuRF': getattr(skrebate, 'TuRF'),
                'xgboost_XGBClassifier' : getattr(xgboost, 'XGBClassifier'),
                'xgboost_XGBRegressor' : getattr(xgboost, 'XGBRegressor')
            }
            syms.update(estimator_table)

        for key in unwanted:
            syms.pop(key, None)

        super(SafeEval, self).__init__(symtable=syms, use_numpy=False, minimal=False,
                                        no_if=True, no_for=True, no_while=True, no_try=True,
                                        no_functiondef=True, no_ifexp=True, no_listcomp=False,
                                        no_augassign=False, no_assert=True, no_delete=True,
                                        no_raise=True, no_print=True)


def get_search_params(params_builder):
    search_params = {}
    safe_eval = SafeEval(load_scipy=True, load_numpy=True)
    safe_eval_es = SafeEval(load_estimators=True)

    for p in params_builder['param_set']:
        search_p = p['search_param_selector']['search_p']
        if search_p.strip() == '':
            continue
        param_type = p['search_param_selector']['selected_param_type']

        lst = search_p.split(":")
        assert (len(lst) == 2), "Error, make sure there is one and only one colon in search parameter input."
        literal = lst[1].strip()
        param_name = lst[0].strip()
        if param_name:
            if param_name.lower() == 'n_jobs':
                sys.exit("Parameter `%s` is invalid for search." %param_name)
            elif not param_name.endswith('-'):
                ev = safe_eval(literal)
                if param_type == "final_estimator_p":
                    search_params["estimator__" + param_name] = ev
                else:
                    search_params["preprocessing_" + param_type[5:6] + "__" + param_name] = ev
            else:
                # only for estimator eval, add `-` to the end of param
                #TODO maybe add regular express check
                ev = safe_eval_es(literal)
                for obj in ev:
                    if 'n_jobs' in obj.get_params():
                        obj.set_params( n_jobs=N_JOBS )
                if param_type == "final_estimator_p":
                    search_params["estimator__" + param_name[:-1]] = ev
                else:
                    search_params["preprocessing_" + param_type[5:6] + "__" + param_name[:-1]] = ev
        elif param_type != "final_estimator_p":
            #TODO regular express check ?
            ev = safe_eval_es(literal)
            preprocessors = [preprocessing.StandardScaler(), preprocessing.Binarizer(), preprocessing.Imputer(),
                            preprocessing.MaxAbsScaler(), preprocessing.Normalizer(), preprocessing.MinMaxScaler(),
                            preprocessing.PolynomialFeatures(),preprocessing.RobustScaler(),
                            feature_selection.SelectKBest(), feature_selection.GenericUnivariateSelect(),
                            feature_selection.SelectPercentile(), feature_selection.SelectFpr(), feature_selection.SelectFdr(),
                            feature_selection.SelectFwe(), feature_selection.VarianceThreshold(),
                            decomposition.FactorAnalysis(random_state=0), decomposition.FastICA(random_state=0), decomposition.IncrementalPCA(),
                            decomposition.KernelPCA(random_state=0, n_jobs=N_JOBS), decomposition.LatentDirichletAllocation(random_state=0, n_jobs=N_JOBS),
                            decomposition.MiniBatchDictionaryLearning(random_state=0, n_jobs=N_JOBS),
                            decomposition.MiniBatchSparsePCA(random_state=0, n_jobs=N_JOBS), decomposition.NMF(random_state=0),
                            decomposition.PCA(random_state=0), decomposition.SparsePCA(random_state=0, n_jobs=N_JOBS),
                            decomposition.TruncatedSVD(random_state=0),
                            kernel_approximation.Nystroem(random_state=0), kernel_approximation.RBFSampler(random_state=0),
                            kernel_approximation.AdditiveChi2Sampler(), kernel_approximation.SkewedChi2Sampler(random_state=0),
                            cluster.FeatureAgglomeration(),
                            skrebate.ReliefF(n_jobs=N_JOBS), skrebate.SURF(n_jobs=N_JOBS), skrebate.SURFstar(n_jobs=N_JOBS),
                            skrebate.MultiSURF(n_jobs=N_JOBS), skrebate.MultiSURFstar(n_jobs=N_JOBS),
                            imblearn.under_sampling.ClusterCentroids(random_state=0, n_jobs=N_JOBS),
                            imblearn.under_sampling.CondensedNearestNeighbour(random_state=0, n_jobs=N_JOBS),
                            imblearn.under_sampling.EditedNearestNeighbours(random_state=0, n_jobs=N_JOBS),
                            imblearn.under_sampling.RepeatedEditedNearestNeighbours(random_state=0, n_jobs=N_JOBS),
                            imblearn.under_sampling.AllKNN(random_state=0, n_jobs=N_JOBS),
                            imblearn.under_sampling.InstanceHardnessThreshold(random_state=0, n_jobs=N_JOBS),
                            imblearn.under_sampling.NearMiss(random_state=0, n_jobs=N_JOBS),
                            imblearn.under_sampling.NeighbourhoodCleaningRule(random_state=0, n_jobs=N_JOBS),
                            imblearn.under_sampling.OneSidedSelection(random_state=0, n_jobs=N_JOBS),
                            imblearn.under_sampling.RandomUnderSampler(random_state=0),
                            imblearn.under_sampling.TomekLinks(random_state=0, n_jobs=N_JOBS),
                            imblearn.over_sampling.ADASYN(random_state=0, n_jobs=N_JOBS),
                            imblearn.over_sampling.RandomOverSampler(random_state=0),
                            imblearn.over_sampling.SMOTE(random_state=0, n_jobs=N_JOBS),
                            imblearn.over_sampling.SVMSMOTE(random_state=0, n_jobs=N_JOBS),
                            imblearn.over_sampling.BorderlineSMOTE(random_state=0, n_jobs=N_JOBS),
                            imblearn.over_sampling.SMOTENC(categorical_features=[], random_state=0, n_jobs=N_JOBS),
                            imblearn.combine.SMOTEENN(random_state=0), imblearn.combine.SMOTETomek(random_state=0)]
            newlist = []
            for obj in ev:
                if obj is None:
                    newlist.append(None)
                elif obj == 'all_0':
                    newlist.extend(preprocessors[0:36])
                elif obj == 'sk_prep_all':      # no KernalCenter()
                    newlist.extend(preprocessors[0:8])
                elif obj == 'fs_all':
                    newlist.extend(preprocessors[8:15])
                elif obj == 'decomp_all':
                    newlist.extend(preprocessors[15:26])
                elif obj == 'k_appr_all':
                    newlist.extend(preprocessors[26:30])
                elif obj == "reb_all":
                    newlist.extend(preprocessors[31:36])
                elif obj == 'imb_all':
                    newlist.extend(preprocessors[36:55])
                elif  type(obj) is int and -1 < obj < len(preprocessors):
                    newlist.append(preprocessors[obj])
                elif hasattr(obj, 'get_params'):       # user object
                    if 'n_jobs' in obj.get_params():
                        newlist.append( obj.set_params(n_jobs=N_JOBS) )
                    else:
                        newlist.append(obj)
                else:
                    sys.exit("Unsupported preprocessor type: %r" %(obj))
            search_params["preprocessing_" + param_type[5:6]] = newlist
        else:
            sys.exit("Parameter name of the final estimator can't be skipped!")

    return search_params


def get_estimator(estimator_json):
    estimator_module = estimator_json['selected_module']
    estimator_cls = estimator_json['selected_estimator']

    if estimator_module == "xgboost":
        cls = getattr(xgboost, estimator_cls)
    else:
        module = getattr(sklearn, estimator_module)
        cls = getattr(module, estimator_cls)

    estimator = cls()

    estimator_params = estimator_json['text_params'].strip()
    if estimator_params != "":
        try:
            params = safe_eval('dict(' + estimator_params + ')')
        except ValueError:
            sys.exit("Unsupported parameter input: `%s`" % estimator_params)
        estimator.set_params(**params)
    if 'n_jobs' in estimator.get_params():
        estimator.set_params(n_jobs=N_JOBS)

    return estimator


def get_cv(cv_json):
    """
    cv_json:
            e.g.:
            {
                "selected_cv": "StratifiedKFold",
                "n_splits": 3,
                "shuffle": True,
                "random_state": 0
            }
    """
    cv = cv_json.pop('selected_cv')
    if cv == "default":
        return cv_json['n_splits'], None

    groups = cv_json.pop('groups', None)
    if groups:
        groups = groups.strip()
        if groups != "":
            if groups.startswith("__ob__"):
                groups = groups[6:]
            if groups.endswith("__cb__"):
                groups = groups[:-6]
            groups = [int(x.strip()) for x in groups.split(',')]

    for k, v in cv_json.items():
        if v == "":
            cv_json[k] = None

    test_fold = cv_json.get('test_fold', None)
    if test_fold:
        if test_fold.startswith("__ob__"):
            test_fold = test_fold[6:]
        if test_fold.endswith("__cb__"):
            test_fold = test_fold[:-6]
        cv_json['test_fold'] = [int(x.strip()) for x in test_fold.split(',')]

    test_size = cv_json.get('test_size', None)
    if test_size and test_size > 1.0:
        cv_json['test_size'] = int(test_size)

    cv_class = getattr(model_selection, cv)
    splitter = cv_class(**cv_json)

    return splitter, groups


def get_scoring(scoring_json):
    def balanced_accuracy_score(y_true, y_pred):
        C = metrics.confusion_matrix(y_true, y_pred)
        with np.errstate(divide='ignore', invalid='ignore'):
            per_class = np.diag(C) / C.sum(axis=1)
        if np.any(np.isnan(per_class)):
            warnings.warn('y_pred contains classes not in y_true')
            per_class = per_class[~np.isnan(per_class)]
        score = np.mean(per_class)
        return score

    if scoring_json['primary_scoring'] == "default":
        return None

    my_scorers = metrics.SCORERS
    if 'balanced_accuracy' not in my_scorers:
        my_scorers['balanced_accuracy'] = metrics.make_scorer(balanced_accuracy_score)

    if scoring_json['secondary_scoring'] != 'None'\
            and scoring_json['secondary_scoring'] != scoring_json['primary_scoring']:
        scoring = {}
        scoring['primary'] = my_scorers[scoring_json['primary_scoring']]
        for scorer in scoring_json['secondary_scoring'].split(','):
            if scorer != scoring_json['primary_scoring']:
                scoring[scorer] = my_scorers[scorer]
        return scoring

    return my_scorers[scoring_json['primary_scoring']]
