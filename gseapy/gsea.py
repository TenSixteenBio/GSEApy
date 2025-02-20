#! python
# -*- coding: utf-8 -*-

import glob
import logging
import os
import xml.etree.ElementTree as ET
from collections import Counter
from typing import Dict, List, Optional, Tuple, Union

import numpy as np
import pandas as pd

from gseapy.base import GSEAbase
from gseapy.gse import CorrelType, Metric, gsea_rs, prerank2d_rs, prerank_rs, ssgsea_rs
from gseapy.parser import gsea_cls_parser
from gseapy.plot import gseaplot
from gseapy.utils import mkdirs

# from memory_profiler import profile


class GSEA(GSEAbase):
    """GSEA main tool"""

    def __init__(
        self,
        data: Union[pd.DataFrame, str],
        gene_sets: Union[List[str], str, Dict[str, str]],
        classes: Union[List[str], str, Dict[str, str]],
        outdir: Optional[str] = None,
        min_size: int = 15,
        max_size: int = 500,
        permutation_num: int = 1000,
        weight: float = 1.0,
        permutation_type: str = "phenotype",
        method: str = "signal_to_noise",
        ascending: bool = False,
        threads: int = 1,
        figsize: Tuple[float, float] = (6.5, 6),
        format: str = "pdf",
        graph_num: int = 20,
        no_plot: bool = False,
        seed: int = 123,
        verbose: bool = False,
    ):
        super(GSEA, self).__init__(
            outdir=outdir,
            gene_sets=gene_sets,
            module="gsea",
            threads=threads,
            verbose=verbose,
        )
        self.data = data
        self.classes = classes
        self.permutation_type = permutation_type
        self.method = method
        self.min_size = min_size
        self.max_size = max_size
        self.permutation_num = int(permutation_num) if int(permutation_num) > 0 else 0
        self.weight = weight
        self.ascending = ascending
        self.figsize = figsize
        self.format = format
        self.graph_num = int(graph_num)
        self.seed = seed
        self.ranking = None
        self._noplot = no_plot
        self.pheno_pos = "pos"
        self.pheno_neg = "neg"

    def load_data(self, cls_vec: List[str]) -> Tuple[pd.DataFrame, Dict]:
        """pre-processed the data frame.new filtering methods will be implement here."""
        # read data in
        if isinstance(self.data, pd.DataFrame):
            exprs = self.data.copy()
            # handle index is gene_names
            if exprs.index.dtype == "O":
                exprs = exprs.reset_index()
        elif os.path.isfile(self.data):
            # GCT input format?
            if self.data.endswith("gct"):
                exprs = pd.read_csv(self.data, skiprows=2, sep="\t")
            else:
                exprs = pd.read_csv(self.data, comment="#", sep="\t")
        else:
            raise Exception("Error parsing gene expression DataFrame!")

        # drop duplicated gene names
        if exprs.iloc[:, 0].duplicated().sum() > 0:
            self._logger.warning(
                "Dropping duplicated gene names, only keep the first values"
            )
            # drop duplicate gene_names.
            exprs.drop_duplicates(subset=exprs.columns[0], inplace=True)
        if exprs.isnull().any().sum() > 0:
            self._logger.warning("Input data contains NA, filled NA with 0")
            exprs.dropna(how="all", inplace=True)  # drop rows with all NAs
            exprs = exprs.fillna(0)
        # set gene name as index
        exprs.set_index(keys=exprs.columns[0], inplace=True)
        # select numberic columns
        df = exprs.select_dtypes(include=[np.number])

        # in case the description column is numeric
        if len(cls_vec) == (df.shape[1] - 1):
            df = df.iloc[:, 1:]
        # drop gene which std == 0 in all samples
        cls_dict = {k: v for k, v in zip(df.columns, cls_vec)}
        # compatible to py3.7
        major, minor, _ = [int(i) for i in pd.__version__.split(".")]
        if (major == 1 and minor < 5) or (major < 1):
            # fix numeric_only error
            df_std = df.groupby(by=cls_dict, axis=1).std()
        else:
            df_std = df.groupby(by=cls_dict, axis=1).std(numeric_only=True)
        df = df[df_std.sum(axis=1) > 0]
        df = df + 1e-08  # we don't like zeros!!!

        return df, cls_dict

    def calculate_metric(
        self,
        df: pd.DataFrame,
        method: str,
        pos: str,
        neg: str,
        classes: Dict[str, List[str]],
        ascending: bool,
    ) -> Tuple[List[int], pd.Series]:
        """The main function to rank an expression table. works for 2d array.

        :param df:      gene_expression DataFrame.
        :param method:  The method used to calculate a correlation or ranking. Default: 'log2_ratio_of_classes'.
                        Others methods are:

                        1. 'signal_to_noise' (s2n) or 'abs_signal_to_noise' (abs_s2n)

                            You must have at least three samples for each phenotype.
                            The more distinct the gene expression is in each phenotype,
                            the more the gene acts as a “class marker”.

                        2. 't_test'

                            Uses the difference of means scaled by the standard deviation and number of samples.
                            Note: You must have at least three samples for each phenotype to use this metric.
                            The larger the t-test ratio, the more distinct the gene expression is in each phenotype
                            and the more the gene acts as a “class marker.”

                        3. 'ratio_of_classes' (also referred to as fold change).

                            Uses the ratio of class means to calculate fold change for natural scale data.

                        4. 'diff_of_classes'

                            Uses the difference of class means to calculate fold change for natural scale data

                        5. 'log2_ratio_of_classes'

                            Uses the log2 ratio of class means to calculate fold change for natural scale data.
                            This is the recommended statistic for calculating fold change for log scale data.

        :param str pos: one of labels of phenotype's names.
        :param str neg: one of labels of phenotype's names.
        :param dict classes: column id to group mapping.
        :param bool ascending:  bool or list of bool. Sort ascending vs. descending.
        :return: returns argsort values of a tuple where
            0: argsort positions (indices)
            1: pd.Series of correlation value. Gene_name is index, and value is rankings.

        visit here for more docs: http://software.broadinstitute.org/gsea/doc/GSEAUserGuideFrame.html
        """

        # exclude any zero stds.
        # compatible to py3.7
        major, minor, _ = [int(i) for i in pd.__version__.split(".")]
        if (major == 1 and minor < 5) or (major < 1):
            # fix numeric_only error
            df_mean = df.groupby(by=classes, axis=1).mean()
            df_std = df.groupby(by=classes, axis=1).std()
        else:
            df_mean = df.groupby(by=classes, axis=1).mean(numeric_only=True)
            df_std = df.groupby(by=classes, axis=1).std(numeric_only=True)
        class_values = Counter(classes.values())
        n_pos = class_values[pos]
        n_neg = class_values[neg]

        if method in ["signal_to_noise", "s2n"]:
            ser = (df_mean[pos] - df_mean[neg]) / (df_std[pos] + df_std[neg])
        elif method in ["abs_signal_to_noise", "abs_s2n"]:
            ser = ((df_mean[pos] - df_mean[neg]) / (df_std[pos] + df_std[neg])).abs()
        elif method == "t_test":
            ser = (df_mean[pos] - df_mean[neg]) / np.sqrt(
                df_std[pos] ** 2 / n_pos + df_std[neg] ** 2 / n_neg
            )
        elif method == "ratio_of_classes":
            ser = df_mean[pos] / df_mean[neg]
        elif method == "diff_of_classes":
            ser = df_mean[pos] - df_mean[neg]
        elif method == "log2_ratio_of_classes":
            ser = np.log2(df_mean[pos] / df_mean[neg])
        else:
            logging.error("Please provide correct method name!!!")
            raise LookupError("Input method: %s is not supported" % method)
        ser_ind = ser.values.argsort().tolist()
        ser = ser.iloc[ser_ind]
        if ascending:
            return ser_ind, ser
        # descending order
        return ser_ind[::-1], ser[::-1]

    def load_classes(
        self,
    ):
        """Parse group (classes)"""
        if isinstance(self.classes, dict):
            # check number of samples
            class_values = Counter(self.classes.values())
            s = []
            for c, v in sorted(class_values.items(), key=lambda item: item[1]):
                if v < 3:
                    raise Exception(f"Number of {c}: {v}, it must be >= 3!")
                s.append(c)
            self.pheno_pos = s[0]
            self.pheno_neg = s[1]
            # n_pos = class_values[pos]
            # n_neg = class_values[neg]
            return
        else:
            pos, neg, cls_vector = gsea_cls_parser(self.classes)
            self.pheno_pos = pos
            self.pheno_neg = neg
            return cls_vector

    # @profile
    def run(self):
        """GSEA main procedure"""
        m = self.method.lower()
        if m in ["signal_to_noise", "s2n"]:
            method = Metric.Signal2Noise
        elif m in ["s2n", "abs_signal_to_noise", "abs_s2n"]:
            method = Metric.AbsSignal2Noise
        elif m == "t_test":
            method = Metric.Ttest
        elif m == "ratio_of_classes":
            method = Metric.RatioOfClasses
        elif m == "diff_of_classes":
            method = Metric.DiffOfClasses
        elif m == "log2_ratio_of_classes":
            method = Metric.Log2RatioOfClasses
        else:
            raise Exception("Sorry, input method %s is not supported" % m)

        assert self.permutation_type in ["phenotype", "gene_set"]
        assert self.min_size <= self.max_size

        # Start Analysis
        self._logger.info("Parsing data files for GSEA.............................")
        # phenotype labels parsing
        cls_vector = self.load_classes()
        # select correct expression genes and values.
        dat, cls_dict = self.load_data(cls_vector)
        self.cls_dict = cls_dict
        # data frame must have length > 1
        assert len(dat) > 1
        # filtering out gene sets and build gene sets dictionary
        gmt = self.load_gmt(gene_list=dat.index.values, gmt=self.gene_sets)
        self.gmt = gmt
        self._logger.info(
            "%04d gene_sets used for further statistical testing....." % len(gmt)
        )
        self._logger.info("Start to run GSEA...Might take a while..................")
        # cpu numbers
        # compute ES, NES, pval, FDR, RES
        if self.permutation_type == "gene_set":
            # ranking metrics calculation.
            idx, dat2 = self.calculate_metric(
                df=dat,
                method=self.method,
                pos=self.pheno_pos,
                neg=self.pheno_neg,
                classes=cls_dict,
                ascending=self.ascending,
            )
            gsum = prerank_rs(
                dat2.index.values.tolist(),  # gene list
                dat2.squeeze().values.tolist(),  # ranking values
                gmt,  # must be a dict object
                self.weight,
                self.min_size,
                self.max_size,
                self.permutation_num,
                self._threads,
                self.seed,
            )
            ## need to update indices, prerank_rs only stores input's order
            # so compatible with code code below
            indices = gsum.indices
            indices[0] = idx
            gsum.indices = indices  # only accept [[]]
        else:  # phenotype permutation
            group = list(
                map(lambda x: True if x == self.pheno_pos else False, cls_vector)
            )
            gsum = gsea_rs(
                dat.index.values.tolist(),
                dat.values.tolist(),  # each row is gene values across samples
                gmt,
                group,
                method,
                self.weight,
                self.min_size,
                self.max_size,
                self.permutation_num,
                self._threads,
                self.seed,
            )

        if self._outdir is not None:
            self._logger.info(
                "Start to generate GSEApy reports and figures............"
            )
        self.ranking = pd.Series(gsum.rankings[0], index=dat.index[gsum.indices[0]])
        # reorder datarame for heatmap
        # self._heatmat(df=dat.loc[dat2.index], classes=cls_vector)
        self._heatmat(df=dat.iloc[gsum.indices[0]], classes=cls_vector)
        # write output and plotting
        self.to_df(gsum.summaries, gmt, self.ranking)
        self._logger.info("Congratulations. GSEApy ran successfully.................\n")

        return


class Prerank(GSEAbase):
    """GSEA prerank tool"""

    def __init__(
        self,
        rnk: Union[pd.DataFrame, pd.Series, str],
        gene_sets: Union[List[str], str, Dict[str, str]],
        outdir: Optional[str] = None,
        pheno_pos="Pos",
        pheno_neg="Neg",
        min_size: int = 15,
        max_size: int = 500,
        permutation_num: int = 1000,
        weight: float = 1.0,
        ascending: bool = False,
        threads: int = 1,
        figsize: Tuple[float, float] = (6.5, 6),
        format: str = "pdf",
        graph_num: int = 20,
        no_plot: bool = False,
        seed: int = 123,
        verbose: bool = False,
    ):
        super(Prerank, self).__init__(
            outdir=outdir,
            gene_sets=gene_sets,
            module="prerank",
            threads=threads,
            verbose=verbose,
        )
        self.rnk = rnk
        self.pheno_pos = pheno_pos
        self.pheno_neg = pheno_neg
        self.min_size = min_size
        self.max_size = max_size
        self.permutation_num = int(permutation_num) if int(permutation_num) > 0 else 0
        self.weight = weight
        self.ascending = ascending
        self.figsize = figsize
        self.format = format
        self.graph_num = int(graph_num)
        self.seed = seed
        self.ranking = None
        self._noplot = no_plot
        self.permutation_type = "gene_set"

    def load_ranking(self):
        ranking = self._load_ranking(self.rnk)  # index is gene_names
        if isinstance(ranking, pd.DataFrame):
            # handle index is not gene_names
            if ranking.index.dtype != "O":
                ranking.set_index(keys=ranking.columns[0], inplace=True)
            # handle columns names are intergers
            if ranking.columns.dtype != "O":
                ranking.columns = ranking.columns.astype(str)
            # drop na values
            ranking = ranking.loc[ranking.index.dropna()]
            if ranking.isnull().any().sum() > 0:
                self._logger.warning("Input rankings contains NA values!")
                # fill na
                ranking.dropna(how="all", inplace=True)
                ranking.fillna(0, inplace=True)
            # drop duplicate IDs, keep the first
            if ranking.index.duplicated().sum() > 0:
                self._logger.warning("Duplicated ID detected!")
                ranking = ranking.loc[~ranking.index.duplicated(keep="first"), :]

            # check whether contains infinity values
            if np.isinf(ranking).values.sum() > 0:
                self._logger.warning("Input gene rankings contains inf values!")
                col_min_max = {
                    np.inf: ranking[np.isfinite(ranking)].max(),  # column-wise max
                    -np.inf: ranking[np.isfinite(ranking)].min(),
                }  # column-wise min
                ranking = ranking.replace({col: col_min_max for col in ranking.columns})

            # check ties in prerank stats
            dups = ranking.apply(lambda df: df.duplicated().sum() / df.size)
            if (dups > 0).sum() > 0:
                msg = (
                    "Duplicated values found in preranked stats:\nsample\tratio\n%s\n"
                    % (dups.to_string(float_format="{:,.2%}".format))
                )
                msg += "The order of those genes will be arbitrary, which may produce unexpected results."
                self._logger.warning(msg)

        return ranking

    # @profile
    def run(self):
        """GSEA prerank workflow"""

        assert self.min_size <= self.max_size

        # parsing rankings
        dat2 = self.load_ranking()
        assert len(dat2) > 1
        self.ranking = dat2
        # Start Analysis
        self._logger.info("Parsing data files for GSEA.............................")
        # filtering out gene sets and build gene sets dictionary
        gmt = self.load_gmt(gene_list=dat2.index.values, gmt=self.gene_sets)
        self.gmt = gmt
        self._logger.info(
            "%04d gene_sets used for further statistical testing....." % len(gmt)
        )
        self._logger.info("Start to run GSEA...Might take a while..................")
        # compute ES, NES, pval, FDR, RES
        if isinstance(dat2, pd.DataFrame):
            _prerank = prerank2d_rs
        else:
            _prerank = prerank_rs
        # run
        gsum = _prerank(
            dat2.index.values.tolist(),  # gene list
            dat2.values.tolist(),  # ranking values
            gmt,  # must be a dict object
            self.weight,
            self.min_size,
            self.max_size,
            self.permutation_num,
            self._threads,
            self.seed,
        )
        self.to_df(
            gsea_summary=gsum.summaries,
            gmt=gmt,
            rank_metric=dat2,
            indices=gsum.indices if isinstance(dat2, pd.DataFrame) else None,
        )
        if self._outdir is not None:
            self._logger.info(
                "Start to generate gseapy reports, and produce figures..."
            )

        self._logger.info("Congratulations. GSEApy runs successfully................\n")

        return


class SingleSampleGSEA(GSEAbase):
    """GSEA extension: single sample GSEA"""

    def __init__(
        self,
        data: Union[pd.DataFrame, pd.Series, str],
        gene_sets: Union[List[str], str, Dict[str, str]],
        outdir: Optional[str] = None,
        sample_norm_method: str = "rank",
        correl_norm_type: str = "zscore",
        min_size: int = 15,
        max_size: int = 500,
        permutation_num: Optional[int] = None,
        weight: float = 0.25,
        ascending: bool = False,
        threads: int = 1,
        figsize: Tuple[float, float] = (6.5, 6),
        format: str = "pdf",
        graph_num: int = 20,
        no_plot: bool = True,
        seed: int = 123,
        verbose: bool = False,
        **kwargs,
    ):
        super(SingleSampleGSEA, self).__init__(
            outdir=outdir,
            gene_sets=gene_sets,
            module="ssgsea",
            threads=threads,
            verbose=verbose,
        )
        self.data = data
        self.sample_norm_method = sample_norm_method
        self.correl_type = self.norm_correl(correl_norm_type)
        self.weight = weight
        self.min_size = min_size
        self.max_size = max_size
        self.permutation_num = int(permutation_num) if permutation_num else None
        self.ascending = ascending
        self.figsize = figsize
        self.format = format
        self.graph_num = int(graph_num)
        self.seed = seed
        self.ranking = None
        self._noplot = no_plot
        self.permutation_type = "gene_set"

    def corplot(self):
        """NES Correlation plot
        TODO
        """

    def setplot(self):
        """ranked genes' location plot
        TODO
        """

    def load_data(self) -> pd.DataFrame:
        # load data
        exprs = self.data
        if isinstance(exprs, pd.DataFrame):
            rank_metric = exprs.copy()
            # handle dataframe with gene_name as index.
            self._logger.debug("Input data is a DataFrame with gene names")
            # handle index is not gene_names
            if rank_metric.index.dtype != "O":
                rank_metric.set_index(keys=rank_metric.columns[0], inplace=True)
            if rank_metric.columns.dtype != "O":
                rank_metric.columns = rank_metric.columns.astype(str)

            rank_metric = rank_metric.select_dtypes(include=[np.number])
        elif isinstance(exprs, pd.Series):
            # change to DataFrame
            self._logger.debug("Input data is a Series with gene names")
            if exprs.name is None:
                # rename col if name attr is none
                exprs.name = "sample1"
            elif exprs.name.dtype != "O":
                exprs.name = exprs.name.astype(str)
            rank_metric = exprs.to_frame()
        elif os.path.isfile(exprs):
            # GCT input format?
            if exprs.endswith("gct"):
                rank_metric = pd.read_csv(
                    exprs, skiprows=1, comment="#", index_col=0, sep="\t"
                )
            else:
                # just txt file like input
                rank_metric = pd.read_csv(exprs, comment="#", index_col=0, sep="\t")
                if rank_metric.shape[1] == 1:
                    # rnk file like input
                    rank_metric.columns = rank_metric.columns.astype(str)
            # select numbers
            rank_metric = rank_metric.select_dtypes(include=[np.number])
        else:
            raise Exception("Error parsing gene ranking values!")
        if rank_metric.index.duplicated().sum() > 0:
            self._logger.warning(
                "Dropping duplicated gene names, values averaged by gene names!"
            )
            rank_metric = rank_metric.loc[rank_metric.index.dropna()]
            rank_metric = rank_metric.groupby(level=0).mean()
        if rank_metric.isnull().any().sum() > 0:
            self._logger.warning("Input data contains NA, filled NA with 0")
            rank_metric = rank_metric.fillna(0)

        return rank_metric

    def norm_samples(self, dat: pd.DataFrame) -> pd.DataFrame:
        """normalization samples
        see here: https://github.com/broadinstitute/ssGSEA2.0/blob/f682082f62ae34185421545f25041bae2c78c89b/src/ssGSEA2.0.R#L237
        """

        if self.sample_norm_method == "rank":
            data = dat.rank(axis=0, method="average", na_option="bottom")
            data = 10000 * data / data.shape[0]
        elif self.sample_norm_method == "log_rank":
            data = dat.rank(axis=0, method="average", na_option="bottom")
            data = np.log(10000 * data / data.shape[0] + np.exp(1))
        elif self.sample_norm_method == "log":
            dat[dat < 1] = 1
            data = np.log(dat + np.exp(1))
        elif self.sample_norm_method == "custom":
            self._logger.info("Use custom rank metric for ssGSEA")
            data = dat
        else:
            raise Exception("No supported method: %s" % self.sample_norm_method)

        return data

    def norm_correl(self, cortype):
        """
        After norm_samples, input value will be further nomizalized before using them to calculate scores.
        see source code:
        https://github.com/broadinstitute/ssGSEA2.0/blob/f682082f62ae34185421545f25041bae2c78c89b/src/ssGSEA2.0.R#L396
        """
        if cortype == "zscore":
            return CorrelType.ZScore
        elif cortype == "rank":
            return CorrelType.Rank
        elif cortype == "symrank":
            return CorrelType.SymRank
        else:
            raise Exception("unsupported correl type input")

    def run(self):
        """run entry"""
        if self.permutation_num is None:
            self.permutation_num = 0
        self._logger.info("Parsing data files for ssGSEA...........................")
        # load data
        data = self.load_data()
        # normalized samples, and rank
        normdat = self.norm_samples(data)
        self.ranking = normdat
        # filtering out gene sets and build gene sets dictionary
        gmt = self.load_gmt(gene_list=normdat.index.values, gmt=self.gene_sets)
        self.gmt = gmt
        self._logger.info(
            "%04d gene_sets used for further statistical testing....." % len(gmt)
        )
        # start analysis
        self._logger.info("Start to run ssGSEA...Might take a while................")
        if self.permutation_num > 0:
            # run permutation procedure and calculate pvals, fdrs
            self._logger.warning(
                "Run ssGSEA with permutation procedure, don't use the pval, fdr results for publication."
            )
        self.runSamplesPermu(df=normdat, gmt=gmt)

    def runSamplesPermu(
        self, df: pd.DataFrame, gmt: Optional[Dict[str, List[str]]] = None
    ):
        """Single Sample GSEA workflow with permutation procedure"""

        assert self.min_size <= self.max_size
        if self._outdir:
            mkdirs(self.outdir)
        gsum = ssgsea_rs(
            df.index.values.tolist(),
            df.values.tolist(),
            gmt,
            self.weight,
            self.min_size,
            self.max_size,
            self.permutation_num,  # permutate just like runing prerank analysis
            self.correl_type,
            self._threads,
            self.seed,
        )
        self.to_df(gsum.summaries, gmt, df, gsum.indices)
        return


class Replot(GSEAbase):
    """To reproduce GSEA desktop output results."""

    def __init__(
        self,
        indir: str,
        outdir: str = "GSEApy_Replot",
        weight: float = 1.0,
        min_size: int = 3,
        max_size: int = 1000,
        figsize: Tuple[float, float] = (6.5, 6),
        format: str = "pdf",
        verbose: bool = False,
    ):
        self.indir = indir
        self.outdir = outdir
        self.weight = weight
        self.min_size = min_size
        self.max_size = max_size
        self.figsize = figsize
        self.format = format
        self.verbose = bool(verbose)
        self.module = "replot"
        self.gene_sets = None
        self.ascending = False
        # init logger
        self.prepare_outdir()

    def gsea_edb_parser(self, results_path):
        """Parse results.edb file stored under **edb** file folder.

        :param results_path: the path of results.edb file.
        :return:
            a dict contains { enrichment_term: [es, nes, pval, fdr, fwer, hit_ind]}
        """

        xtree = ET.parse(results_path)
        xroot = xtree.getroot()
        res = {}
        # dict_keys(['RANKED_LIST', 'GENESET', 'FWER', 'ES_PROFILE',
        # 'HIT_INDICES', 'ES', 'NES', 'TEMPLATE', 'RND_ES', 'RANK_SCORE_AT_ES',
        # 'NP', 'RANK_AT_ES', 'FDR'])
        for node in xroot.findall("DTG"):
            enrich_term = node.attrib.get("GENESET").split("#")[1]
            es_profile = node.attrib.get("ES_PROFILE").split(" ")
            # esnull = term.get('RND_ES').split(" ")
            hit_ind = node.attrib.get("HIT_INDICES").split(" ")
            es_profile = [float(i) for i in es_profile]
            hit_ind = [int(i) for i in hit_ind]
            # esnull = [float(i) for i in esnull ]
            es = float(node.attrib.get("ES"))
            nes = float(node.attrib.get("NES"))
            pval = float(node.attrib.get("NP"))
            fdr = float(node.attrib.get("FDR"))
            fwer = float(node.attrib.get("FWER"))
            logging.debug("Enriched Gene set is: " + enrich_term)
            res[enrich_term] = [es, nes, pval, fdr, fwer, hit_ind]
        return res

    def run(self):
        """main replot function"""
        assert self.min_size <= self.max_size

        # parsing files.......
        try:
            results_path = glob.glob(os.path.join(self.indir, "edb/results.edb"))[0]
            rank_path = glob.glob(os.path.join(self.indir, "edb/*.rnk"))[0]
            gene_set_path = glob.glob(os.path.join(self.indir, "edb/gene_sets.gmt"))[0]
        except IndexError as e:
            raise Exception("Could not locate GSEA files in the given directory!")
        # extract sample names from .cls file
        cls_path = glob.glob(os.path.join(self.indir, "*/edb/*.cls"))
        if cls_path:
            pos, neg, classes = gsea_cls_parser(cls_path[0])
        else:
            # logic for prerank results
            pos, neg = "", ""
        # start reploting
        self.gene_sets = gene_set_path
        # obtain gene sets
        gene_set_dict = self.parse_gmt(gmt=gene_set_path)
        # obtain rank_metrics
        rank_metric = self._load_ranking(rank_path)
        correl_vector = rank_metric.values
        gene_list = rank_metric.index.values
        # extract each enriment term in the results.edb files and plot.

        database = self.gsea_edb_parser(results_path)
        for enrich_term, data in database.items():
            # extract statistical resutls from results.edb file
            es, nes, pval, fdr, fwer, hit_ind = data
            gene_set = gene_set_dict.get(enrich_term)
            # calculate enrichment score
            RES = self.enrichment_score(
                gene_list=gene_list,
                correl_vector=correl_vector,
                gene_set=gene_set,
                weight=self.weight,
                nperm=0,
            )[-1]
            # plotting
            term = enrich_term.replace("/", "_").replace(":", "_")
            outfile = "{0}/{1}.{2}.{3}".format(
                self.outdir, term, self.module, self.format
            )
            gseaplot(
                rank_metric=rank_metric,
                term=enrich_term,
                hits=hit_ind,
                nes=nes,
                pval=pval,
                fdr=fdr,
                RES=RES,
                pheno_pos=pos,
                pheno_neg=neg,
                figsize=self.figsize,
                ofname=outfile,
            )

        self._logger.info(
            "Congratulations! Your plots have been reproduced successfully!\n"
        )
