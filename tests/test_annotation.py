#!/usr/bin/env python
#  -*- coding: utf-8 -*-

import os
import unittest
import pandas as pd
from utils import *
from beams.grouping import group_features
from beams.in_out import *
from beams.annotation import *


class AnnotationTestCase(unittest.TestCase):

    def setUp(self):

        self.df = combine_peaklist_matrix(to_test_data("peaklist_lcms_pos_theoretical.txt"), to_test_data("dataMatrix_theoretical.txt"))
        self.path, f = os.path.split(os.path.dirname(os.path.abspath(__file__)))

        self.lib_isotopes = read_isotopes(os.path.join(self.path, "beams", "data", "isotopes.txt"), "pos")
        self.lib_adducts = read_adducts(os.path.join(self.path, "beams", "data", "adducts.txt"), "pos")
        self.lib_multiple_charged_ions = read_multiple_charged_ions(os.path.join(self.path, "beams", "data", "multiple_charged_ions.txt"), "pos")
        # lib_mass_differences = read_mass_differences(os.path.join(self.path, "beams", "data", "multiple_charged_differences.txt"), "pos")

        self.db_results = "results_annotation.sqlite"
        self.db_results_graph = "results_annotation_graph.sqlite"
        self.graph = group_features(self.df, to_test_results(self.db_results_graph), max_rt_diff=5.0, coeff_thres=0.7, pvalue_thres=None, method="pearson", block=5000, ncpus=None)

        self.ppm = 2.0

    # def tearDown(self):
    #     os.remove(to_test_results(self.db_results_graph))
    #     os.remove(to_test_results(self.db_results))

    def test_annotate_adducts(self):
        annotate_adducts(self.df, to_test_results(self.db_results), self.ppm, self.lib_adducts)
        self.assertEqual(sqlite_records(to_test_results(self.db_results), "adduct_pairs"), sqlite_records(to_test_data(self.db_results), "adduct_pairs"))

        annotate_adducts(self.graph, to_test_results(self.db_results_graph), self.ppm, self.lib_adducts)
        self.assertEqual(sqlite_records(to_test_results(self.db_results_graph), "adduct_pairs"), sqlite_records(to_test_data(self.db_results_graph), "adduct_pairs"))

    def test_annotate_isotopes(self):
        annotate_isotopes(self.df, to_test_results(self.db_results), self.ppm, self.lib_isotopes)
        self.assertEqual(sqlite_records(to_test_results(self.db_results), "isotopes"), sqlite_records(to_test_data(self.db_results), "isotopes"))
        self.assertEqual(sqlite_count(to_test_results(self.db_results), "isotopes"), 1)

        annotate_isotopes(self.graph, to_test_results(self.db_results_graph), self.ppm, self.lib_isotopes)
        self.assertEqual(sqlite_records(to_test_results(self.db_results_graph), "isotopes"), sqlite_records(to_test_data(self.db_results_graph), "isotopes"))
        self.assertEqual(sqlite_count(to_test_results(self.db_results_graph), "isotopes"), 1)

    def test_annotate_oligomers(self):
        annotate_oligomers(self.df, to_test_results(self.db_results), self.ppm, self.lib_adducts, maximum=5)
        self.assertEqual(sqlite_records(to_test_results(self.db_results), "oligomers"), sqlite_records(to_test_data(self.db_results), "oligomers"))
        self.assertEqual(sqlite_count(to_test_results(self.db_results), "oligomers"), 2)

        annotate_oligomers(self.graph, to_test_results(self.db_results_graph), self.ppm, self.lib_adducts)
        self.assertEqual(sqlite_records(to_test_results(self.db_results_graph), "oligomers"), sqlite_records(to_test_data(self.db_results_graph), "oligomers"))
        self.assertEqual(sqlite_count(to_test_results(self.db_results_graph), "oligomers"), 2)

    def test_annotate_compounds(self):
        db_name = "HMDB"
        fn_sql_db = os.path.join(self.path, "beams", "data", "BEAMS_DB.sqlite")
        annotate_compounds(self.df, self.lib_adducts, self.ppm, to_test_results(self.db_results), fn_sql_db, db_name)
        self.assertEqual(sqlite_records(to_test_results(self.db_results), "compounds_{}".format(db_name)), sqlite_records(to_test_data(self.db_results), "compounds_{}".format(db_name)))
        self.assertEqual(sqlite_count(to_test_results(self.db_results), "compounds_{}".format(db_name)), 51)

    def test_annotate_molecular_formulae(self):
        fn_mf = os.path.join(self.path, "beams", "data", "db_mf.txt")
        annotate_molecular_formulae(self.df, self.lib_adducts, self.ppm, to_test_results(self.db_results), fn_mf)
        self.assertEqual(sqlite_records(to_test_results(self.db_results), "molecular_formulae"), sqlite_records(to_test_data(self.db_results), "molecular_formulae"))
        self.assertEqual(sqlite_count(to_test_results(self.db_results), "molecular_formulae"), 16)

if __name__ == '__main__':
    unittest.main()