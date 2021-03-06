#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os
import time
import itertools
import gzip
import sqlite3
from collections import OrderedDict
from urllib.parse import urlparse
import requests
import pandas as pd
import numpy as np
import networkx as nx
from pyteomics import mass as pyteomics_mass
from beamspy.in_out import read_molecular_formulae
from beamspy.in_out import read_compounds
from beamspy.auxiliary import nist_database_to_pyteomics
from beamspy.auxiliary import composition_to_string


def calculate_mz_tolerance(mass, ppm):
    min_tol = mass - (mass * 0.000001 * ppm)
    max_tol = mass + (mass * 0.000001 * ppm)
    return min_tol, max_tol


def calculate_ppm_error(mass, theo_mass):
    return float(theo_mass - mass) / (theo_mass * 0.000001)


def _remove_elements_from_compositions(records, keep):

    path_nist_database = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'nist_database.txt')
    nist_database = nist_database_to_pyteomics(path_nist_database)

    elements = [e for e in nist_database if e not in keep]
    for record in records:
        for e in elements:
            if "composition" in record:
                record["composition"].pop(e, None)
            else:
                record.pop(e, None)
    return records


def _flatten_composition(records):
    for record in records:
        record.update(record["composition"])
        del record["composition"]
    return records


def _prep_lib(lib):
    lib_pairs = []
    if isinstance(lib, OrderedDict):
        combs = list(itertools.combinations(lib, 2))
        for pair in combs:
            if isinstance(lib[pair[0]], float):
                lib_pairs.append(OrderedDict([(pair[0], {"mass": lib[pair[0]], "charge": 1}),
                                              (pair[1], {"mass": lib[pair[1]], "charge": 1})]))
            else:
                lib_pairs.append(OrderedDict([(pair[0], {"mass": lib[pair[0]]["mass"], "charge": lib[pair[0]]["charge"]}),
                                              (pair[1], {"mass": lib[pair[1]]["mass"], "charge": lib[pair[1]]["charge"]})]))
        lib_pairs = sorted(lib_pairs, key=lambda pair: (list(pair.items())[0][1]["mass"] - list(pair.items())[1][1]["mass"]), reverse=True)
        return lib_pairs
    elif isinstance(lib, list) and isinstance(lib[0], OrderedDict):
        if "mass_difference" in lib[0]:
            return sorted(lib, key=lambda d: d["mass_difference"], reverse=True)
        else:
            raise ValueError("Format library incorrect")
        #else:
        #    return sorted(lib_pairs, key=lambda pair: (list(pair.items())[0][1]["mass"] - list(pair.items())[1][1]["mass"]), reverse=True)
    else:
        raise ValueError("Incorrect format for library: {}".format(type(lib)))


def _annotate_artifacts(peaklist, diff=0.02):
    n = peaklist.iloc[:, 1]
    for i in range(n):
        for j in range(i + 1, n):
            mz_diff = peaklist.iloc[i,1] - peaklist.iloc[j,1]
            ppm_error = calculate_ppm_error(peaklist.iloc[i,1], peaklist.iloc[j,1])
            if abs(mz_diff) < diff:
                yield i, j, mz_diff, ppm_error


def _check_tolerance(mz_x, mz_y, lib_pair, ppm):
    min_tol_a, max_tol_a = calculate_mz_tolerance(mz_x, ppm)
    min_tol_b, max_tol_b = calculate_mz_tolerance(mz_y, ppm)
    if "mass_difference" in lib_pair.keys():
        # Need to fix the order, charge is one
        min_tol_b = (min_tol_b - lib_pair["mass_difference"])
        max_tol_b = (max_tol_b - lib_pair["mass_difference"])
    elif "mass" in list(lib_pair.items())[0][1]:
        # Need to fix the order
        min_tol_a = (min_tol_a - list(lib_pair.items())[0][1]["mass"]) * list(lib_pair.items())[0][1]["charge"]
        max_tol_a = (max_tol_a - list(lib_pair.items())[0][1]["mass"]) * list(lib_pair.items())[0][1]["charge"]

        min_tol_b = (min_tol_b - list(lib_pair.items())[1][1]["mass"]) * list(lib_pair.items())[1][1]["charge"]
        max_tol_b = (max_tol_b - list(lib_pair.items())[1][1]["mass"]) * list(lib_pair.items())[1][1]["charge"]
    else:
        raise ValueError("Incorrect format: {}".format(lib_pair))
    #if min_tol_b > min_tol_a and min_tol_b > max_tol_a:
    #    return -1

    # x1 <= mass <= x2
    # y1 <= mass <= y2
    # x1 <= y2 AND y1 <= x2

    if min_tol_a < max_tol_b and min_tol_b < max_tol_a:
        return 1

    return 0


def _annotate_pairs_from_graph(G, ppm, lib_pairs):

    for e in G.edges(data=True):
        #if G.nodes[e[0]]["mz"] < G.nodes[e[1]]["mz"]:
        #    mz_x = G.nodes[e[0]]["mz"]
        #    mz_y = G.nodes[e[1]]["mz"]
        #else:
        mz_x = G.nodes[e[0]]["mz"]
        mz_y = G.nodes[e[1]]["mz"]

        for lib_pair in lib_pairs:
            ct = _check_tolerance(mz_x, mz_y, lib_pair, ppm)
            if ct == 1 or ct == True:

                if "charge" in list(lib_pair.items())[0][1]:
                    charge_a = list(lib_pair.items())[0][1]["charge"]
                    charge_b = list(lib_pair.items())[1][1]["charge"]
                else:
                    charge_a = 1
                    charge_b = 1

                if "mass_difference" in lib_pair:
                    ppm_error = calculate_ppm_error(
                        mz_x,
                        mz_y - lib_pair["mass_difference"])
                else:
                    ppm_error = calculate_ppm_error(
                        (mz_x - list(lib_pair.items())[0][1]["mass"]) * charge_a,
                        (mz_y - list(lib_pair.items())[1][1]["mass"]) * charge_b)

                yield OrderedDict([("peak_id_a", e[0]), ("peak_id_b", e[1]),
                                   ("label_a", list(lib_pair.keys())[0]),
                                   ("label_b", list(lib_pair.keys())[1]),
                                   ('charge_a', charge_a),
                                   ('charge_b', charge_b),
                                   ('ppm_error', round(ppm_error, 2))])


def _annotate_pairs_from_peaklist(peaklist, ppm, lib_pairs):
    n = len(peaklist.iloc[:,1])
    for i in range(n):
        for j in range(i + 1, n):

            for lib_pair in lib_pairs:
                ct = _check_tolerance(peaklist.iloc[i,1], peaklist.iloc[j,1], lib_pair, ppm)

                if ct == 1:

                    if "charge" in list(lib_pair.items())[0][1]:
                        charge_a = list(lib_pair.items())[0][1]["charge"]
                        charge_b = list(lib_pair.items())[1][1]["charge"]
                    else:
                        charge_a = 1
                        charge_b = 1

                    if "mass_difference" in lib_pair:
                        ppm_error = calculate_ppm_error(
                            peaklist.iloc[i,1],
                            peaklist.iloc[j,1] - lib_pair["mass_difference"])

                    else:
                        ppm_error = calculate_ppm_error(
                            (peaklist.iloc[i,1] - list(lib_pair.items())[0][1]["mass"]) * list(lib_pair.items())[0][1]["charge"],
                            (peaklist.iloc[j,1] - list(lib_pair.items())[1][1]["mass"]) * list(lib_pair.items())[1][1]["charge"])

                    yield OrderedDict([("peak_id_a", peaklist.iloc[i,0]), ("peak_id_b", peaklist.iloc[j,0]),
                                       ("label_a", list(lib_pair.keys())[0]),
                                       ("label_b", list(lib_pair.keys())[1]),
                                       ('charge_a', charge_a),
                                       ('charge_b', charge_b),
                                       ('ppm_error', round(ppm_error,2))])


class DbCompoundsMemory:

    def __init__(self, filename):

        self.filename = filename
        self.conn = sqlite3.connect(":memory:")
        self.cursor = self.conn.cursor()
        self.cursor.execute("""CREATE TABLE COMPOUNDS(
                            compound_id TEXT PRIMARY KEY  NOT NULL,
                            compound_name TEXT,
                            exact_mass REAL,
                            C INTEGER DEFAULT 0,
                            H INTEGER DEFAULT 0,
                            N INTEGER DEFAULT 0,
                            O INTEGER DEFAULT 0,
                            P INTEGER DEFAULT 0,
                            S INTEGER DEFAULT 0,
                            CHNOPS INTEGER DEFAULT NULL,
                            molecular_formula TEXT DEFAULT NULL
                            );""")

        records = read_compounds(self.filename)
        records = _remove_elements_from_compositions(records, keep=["C", "H", "N", "O", "P", "S"])
        records = _flatten_composition(records)
        for record in records:
            columns = ",".join(map(str, list(record.keys())))
            qms = ', '.join(['?'] * len(record.values()))
            query = """insert into COMPOUNDS ({}) values ({})""".format(columns, qms)
            self.cursor.execute(query, list(record.values()))

        self.cursor.execute("""CREATE INDEX IDX_EXACT_MASS ON COMPOUNDS (exact_mass);""")
        self.conn.commit()

    def select_compounds(self, min_tol, max_tol):
        col_names = ["compound_id", "compound_name", "exact_mass", "C", "H", "N", "O", "P", "S", "CHNOPS", "molecular_formula"]
        self.cursor.execute("""SELECT {} FROM COMPOUNDS WHERE 
                            exact_mass >= {} and exact_mass <= {}
                            """.format(",".join(map(str, col_names)), min_tol, max_tol))
        return [OrderedDict(zip(col_names, list(record))) for record in self.cursor.fetchall()]

    def close(self):
        self.conn.close()


class DbMolecularFormulaeMemory:

    def __init__(self, filename):

        self.filename = filename
        self.conn = sqlite3.connect(":memory:")
        self.cursor = self.conn.cursor()
        self.cursor.execute("""CREATE TABLE MF(
                            exact_mass REAL,
                            C INTEGER DEFAULT 0,
                            H INTEGER DEFAULT 0,
                            N INTEGER DEFAULT 0,
                            O INTEGER DEFAULT 0,
                            P INTEGER DEFAULT 0,
                            S INTEGER DEFAULT 0,
                            CHNOPS INTEGER DEFAULT NULL,
                            HC INTEGER DEFAULT NULL,
                            NOPSC INTEGER DEFAULT NULL,
                            lewis INTEGER DEFAULT NULL,
                            senior INTEGER DEFAULT NULL,
                            double_bond_equivalents REAL,
                            primary key (C,H,N,O,P,S,exact_mass)
                            );""")

        records = read_molecular_formulae(self.filename)
        records = _remove_elements_from_compositions(records, keep=["C", "H", "N", "O", "P", "S"])
        records = _flatten_composition(records)
        for record in records:
            columns = ",".join(map(str, list(record.keys())))
            qms = ', '.join(['?'] * len(record.values()))
            query = """insert into mf ({}) values ({})""".format(columns, qms)
            self.cursor.execute(query, list(record.values()))

        self.cursor.execute("""CREATE INDEX IDX_EXACT_MASS ON MF (exact_mass);""")
        self.cursor.execute("""CREATE INDEX IDX_EXACT_MASS_RULES ON MF (exact_mass, HC, NOPSC, LEWIS, SENIOR);""")
        self.conn.commit()

    def select_mf(self, min_tol, max_tol, rules):

        if rules:
            sql_filters = " and lewis = 1 and senior = 1 and HC = 1 and NOPSC = 1"
        else:
            sql_filters = ""

        col_names = ["exact_mass", "C", "H", "N", "O", "P", "S",
                     "double_bond_equivalents", "LEWIS", "SENIOR", "HC", "NOPSC"]

        self.cursor.execute("""SELECT exact_mass, C, H, N, O, P, S,
                            double_bond_equivalents, LEWIS, SENIOR, HC, NOPSC
                            from mf where exact_mass >= {} and exact_mass <= {}{}
                            """.format(min_tol, max_tol, sql_filters))

        return [OrderedDict(zip(col_names, list(record))) for record in self.cursor.fetchall()]


def annotate_adducts(source, db_out, ppm, lib, add=False):

    conn = sqlite3.connect(db_out)
    cursor = conn.cursor()

    if not add:
        cursor.execute("DROP TABLE IF EXISTS adduct_pairs")

        cursor.execute("""CREATE TABLE adduct_pairs (
                       peak_id_a INTEGER DEFAULT NULL,
                       peak_id_b INTEGER DEFAULT NULL,
                       label_a TEXT DEFAULT NULL,
                       label_b TEXT DEFAULT NULL,
                       ppm_error REAL DEFAULT NULL,
                       PRIMARY KEY (peak_id_a, peak_id_b, label_a, label_b));""")

    lib_pairs = _prep_lib(lib.lib)

    if isinstance(source, nx.classes.digraph.DiGraph):
        source = list(source.subgraph(c) for c in nx.weakly_connected_components(source))

    if isinstance(source, list) and len(source) > 0 and isinstance(source[0], nx.classes.digraph.DiGraph):
        for i, graph in enumerate(source):
            for assignment in _annotate_pairs_from_graph(graph, lib_pairs=lib_pairs, ppm=ppm):
                cursor.execute("""INSERT OR REPLACE into adduct_pairs (peak_id_a, peak_id_b, label_a, label_b, ppm_error)
                               values (?,?,?,?,?)""", (str(assignment["peak_id_a"]), str(assignment["peak_id_b"]),
                                                       assignment["label_a"], assignment["label_b"], float(assignment["ppm_error"])))

    elif isinstance(source, pd.core.frame.DataFrame):
        for assignment in _annotate_pairs_from_peaklist(source, lib_pairs=lib_pairs, ppm=ppm):
            cursor.execute("""INSERT OR REPLACE into adduct_pairs (peak_id_a, peak_id_b, label_a, label_b, ppm_error)
                           values (?,?,?,?,?)""", (assignment["peak_id_a"], assignment["peak_id_b"],
                                                   assignment["label_a"], assignment["label_b"], assignment["ppm_error"]))
    conn.commit()
    conn.close()
    return


def annotate_isotopes(source, db_out, ppm, lib):

    conn = sqlite3.connect(db_out)
    cursor = conn.cursor()

    cursor.execute("DROP TABLE IF EXISTS isotopes")

    cursor.execute("""CREATE TABLE isotopes (
                   peak_id_a INTEGER DEFAULT NULL,
                   peak_id_b INTEGER DEFAULT NULL,
                   label_a TEXT DEFAULT NULL,
                   label_b TEXT DEFAULT NULL,
                   atoms REAL DEFAULT NULL,
                   ppm_error REAL DEFAULT NULL,
                   PRIMARY KEY (peak_id_a, peak_id_b, label_a, label_b));""")

    lib_pairs = _prep_lib(lib.lib)

    abundances = {}
    for pair in lib.lib:
        abundances[list(pair.items())[0][0]] = list(pair.items())[0][1]
        abundances[list(pair.items())[1][0]] = list(pair.items())[1][1]

    if isinstance(source, nx.classes.digraph.DiGraph):
        source = list(source.subgraph(c) for c in nx.weakly_connected_components(source))

    if isinstance(source, list) and len(source) > 0 and isinstance(source[0], nx.classes.digraph.DiGraph):

        for graph in source:

            peaklist = graph.nodes(data=True)

            for assignment in _annotate_pairs_from_graph(graph, lib_pairs=lib_pairs, ppm=ppm):

                y = abundances[assignment["label_a"]]['abundance'] * peaklist[assignment["peak_id_b"]]["intensity"]
                x = abundances[assignment["label_b"]]['abundance'] * peaklist[assignment["peak_id_a"]]["intensity"]

                if x == 0.0 or y == 0.0:
                    atoms = None
                elif abundances[assignment["label_a"]]["abundance"] < abundances[assignment["label_b"]]["abundance"]:
                    atoms = x / y
                else:
                    atoms = y/x

                cursor.execute("""insert into isotopes (peak_id_a, peak_id_b, label_a, label_b, atoms, ppm_error)
                               values (?,?,?,?,?,?)""", (str(assignment["peak_id_a"]), str(assignment["peak_id_b"]),
                               assignment["label_a"], assignment["label_b"], float(atoms), float(assignment["ppm_error"])))

    elif isinstance(source, pd.core.frame.DataFrame):

        for assignment in _annotate_pairs_from_peaklist(source, lib_pairs=lib_pairs, ppm=ppm):

            y = abundances[assignment["label_a"]]["abundance"] * source.loc[source['name'] == assignment["peak_id_b"]]["intensity"].iloc[0]
            x = abundances[assignment["label_b"]]["abundance"] * source.loc[source['name'] == assignment["peak_id_a"]]["intensity"].iloc[0]

            if x == 0.0 or y == 0.0:
                atoms = None
            elif abundances[assignment["label_a"]]["abundance"] < abundances[assignment["label_b"]]["abundance"]:
                atoms = x/y
            else:
                atoms = y/x

            cursor.execute("""insert into isotopes (peak_id_a, peak_id_b, label_a, label_b, atoms, ppm_error)
                           values (?,?,?,?,?,?)""", (assignment["peak_id_a"], assignment["peak_id_b"],
                           assignment["label_a"], assignment["label_b"], atoms, assignment["ppm_error"]))
            conn.commit()

    conn.commit()
    conn.close()
    return


def annotate_oligomers(source, db_out, ppm, lib, maximum=2):

    conn = sqlite3.connect(db_out)
    cursor = conn.cursor()

    cursor.execute("DROP TABLE IF EXISTS oligomers")

    cursor.execute("""CREATE TABLE oligomers (
                   peak_id_a INTEGER DEFAULT NULL,
                   peak_id_b INTEGER DEFAULT NULL,
                   mz_a REAL DEFAULT NULL,
                   mz_b REAL DEFAULT NULL,
                   label_a TEXT DEFAULT NULL,
                   label_b TEXT DEFAULT NULL,
                   mz_ratio REAL DEFAULT NULL,
                   ppm_error REAL DEFAULT NULL,
                   PRIMARY KEY (peak_id_a, peak_id_b));""")

    if isinstance(source, nx.classes.digraph.DiGraph):
        source = list(source.subgraph(c) for c in nx.weakly_connected_components(source))

    if isinstance(source, list) and len(source) > 0 and isinstance(source[0], nx.classes.digraph.DiGraph):

        for graph in source:

            for n in graph.nodes():

                neighbors = list(graph.neighbors(n))

                for d in range(1, len(neighbors)+1):

                    for nn in neighbors:

                        mz_x = graph.nodes[n]["mz"]
                        mz_y = graph.nodes[nn]["mz"]

                        if mz_x < mz_y:

                            for adduct in lib.lib.keys():

                                min_tol_a, max_tol_a = calculate_mz_tolerance(mz_x + ((mz_x - lib.lib[adduct]) * d), ppm)
                                min_tol_b, max_tol_b = calculate_mz_tolerance(mz_y, ppm)

                                if (min_tol_b > max_tol_a and max_tol_b > max_tol_a):# or (min_tol_a < min_tol_b and max_tol_a < min_tol_b):
                                    #print(source.iloc[i][1], source.iloc[j][1], adduct)
                                    break

                                min_tol_a = min_tol_a - lib.lib[adduct]
                                max_tol_a = max_tol_a - lib.lib[adduct]

                                min_tol_b = min_tol_b - lib.lib[adduct]
                                max_tol_b = max_tol_b - lib.lib[adduct]

                                if min_tol_a < max_tol_b and min_tol_b < max_tol_a:

                                    a = (mz_x - lib.lib[adduct]) + (mz_x - lib.lib[adduct]) * d
                                    b = mz_y - lib.lib[adduct]

                                    ratio = (mz_y - lib.lib[adduct]) / (mz_x - lib.lib[adduct])
                                    ppm_error =calculate_ppm_error(a, b)

                                    if "M" in adduct:
                                        adduct_oligo = adduct.replace("M", "{}M".format(int(round(ratio))))
                                    else:
                                        adduct_oligo = "{}{}".format(int(round(ratio)), adduct)

                                    cursor.execute("""insert into oligomers (peak_id_a, peak_id_b, mz_a, mz_b, label_a, label_b, mz_ratio, ppm_error)
                                                   values (?,?,?,?,?,?,?,?)""", (n, nn, mz_x, mz_y, adduct, adduct_oligo, round(ratio, 2), round(ppm_error, 2)))

    elif isinstance(source, pd.core.frame.DataFrame):

        n = len(source.iloc[:,0])
        for adduct in lib.lib.keys():
            for i in range(n):
                for d in range(1, maximum):
                    for j in range(i + 1, n):

                        min_tol_a, max_tol_a = calculate_mz_tolerance(source.iloc[i][1] + ((source.iloc[i][1] - lib.lib[adduct]) * d), ppm)
                        min_tol_b, max_tol_b = calculate_mz_tolerance(source.iloc[j][1], ppm)

                        if (min_tol_b > max_tol_a and max_tol_b > max_tol_a):# or (min_tol_a < min_tol_b and max_tol_a < min_tol_b):
                            #print(source.iloc[i][1], source.iloc[j][1], adduct)
                            break

                        min_tol_a = min_tol_a - lib.lib[adduct]
                        max_tol_a = max_tol_a - lib.lib[adduct]

                        min_tol_b = min_tol_b - lib.lib[adduct]
                        max_tol_b = max_tol_b - lib.lib[adduct]

                        if min_tol_a < max_tol_b and min_tol_b < max_tol_a:

                            a = (source.iloc[i][1] - lib.lib[adduct]) + (source.iloc[i][1] - lib.lib[adduct]) * d
                            b = source.iloc[j][1] - lib.lib[adduct]

                            ratio = (source.iloc[j][1] - lib.lib[adduct]) / (source.iloc[i][1] - lib.lib[adduct])
                            ppm_error = calculate_ppm_error(a, b)

                            if "M" in adduct:
                                adduct_oligo = adduct.replace("M", "{}M".format(int(round(ratio))))
                            else:
                                adduct_oligo = "{}{}".format(int(round(ratio)), adduct)
                            cursor.execute("""insert into oligomers (peak_id_a, peak_id_b, mz_a, mz_b, label_a, label_b, mz_ratio, ppm_error)
                                           values (?,?,?,?,?,?,?,?)""", (source.iloc[i][0], source.iloc[j][0], source.iloc[i][1], source.iloc[j][1], adduct, adduct_oligo, round(ratio, 2), round(ppm_error, 2)))
    conn.commit()
    conn.close()
    return


def annotate_artifacts(source, db_out, diff):

    conn = sqlite3.connect(db_out)
    cursor = conn.cursor()

    cursor.execute("DROP TABLE IF EXISTS artifacts")

    cursor.execute("""CREATE TABLE artifacts (
                   peak_id_a INTEGER DEFAULT NULL,
                   peak_id_b INTEGER DEFAULT NULL,
                   mz_diff REAL DEFAULT NULL,
                   ppm_error REAL DEFAULT NULL,
                   PRIMARY KEY (peak_id_a, peak_id_b));""")

    if isinstance(source, nx.classes.digraph.DiGraph):
        source = list(source.subgraph(c) for c in nx.weakly_connected_components(source))

    if (isinstance(source, list) or isinstance(source, np.ndarray)) and isinstance(source[0], nx.classes.graph.Graph):
        for graph in source:
            peaklist = graph.nodes(data=True)
            for assignment in _annotate_artifacts(peaklist, diff=diff):
                cursor.execute("""insert into artifacts (peak_id_a, peak_id_b, mz_diff, ppm_error)
                               values (?,?,?,?)""", (assignment["peak_id_a"], assignment["peak_id_b"], assignment["label_a"], assignment["label_b"]))

    elif isinstance(source, pd.core.frame.DataFrame):
        for assignment in _annotate_artifacts(source, diff=diff):
            cursor.execute("""insert into artifacts (peak_id_a, peak_id_b, mz_diff, ppm_error)
                           values (?,?,?,?)""", (assignment["peak_id_a"], assignment["peak_id_b"], assignment["label_a"], assignment["label_b"]))

    conn.commit()
    return


def annotate_multiple_charged_ions(source, db_out, ppm, lib, add=False):

    conn = sqlite3.connect(db_out)
    cursor = conn.cursor()

    if not add:
        cursor.execute("DROP TABLE IF EXISTS multiple_charged_ions")

        cursor.execute("""CREATE TABLE multiple_charged_ions (
                       peak_id_a INTEGER DEFAULT NULL,
                       peak_id_b INTEGER DEFAULT NULL,
                       label_a TEXT DEFAULT NULL,
                       label_b TEXT DEFAULT NULL,
                       charge_a INTEGER DEFAULT NULL,
                       charge_b INTEGER DEFAULT NULL,
                       ppm_error REAL DEFAULT NULL,
                       PRIMARY KEY (peak_id_a, peak_id_b, label_a, label_b, charge_a, charge_b));""")

    lib_pairs = _prep_lib(lib.lib)

    if isinstance(source, nx.classes.digraph.DiGraph):
        source = list(source.subgraph(c) for c in nx.weakly_connected_components(source))

    if (isinstance(source, list) or isinstance(source, np.ndarray)) and isinstance(source[0], nx.classes.graph.Graph):
        for graph in source:
            for assignment in _annotate_pairs_from_graph(graph, lib_pairs=lib_pairs, ppm=ppm):
                cursor.execute("""INSERT OR REPLACE into multiple_charged_ions (peak_id_a, peak_id_b, label_a, label_b, charge_a, charge_b, ppm_error)
                               values (?,?,?,?,?,?,?)""", (assignment["peak_id_a"], assignment["peak_id_b"], assignment["label_a"], assignment["label_b"],
                                                           assignment["charge_a"], assignment["charge_b"], assignment["ppm_error"]))

    elif isinstance(source, pd.core.frame.DataFrame):
        for assignment in _annotate_pairs_from_peaklist(source, lib_pairs=lib_pairs, ppm=ppm):
            cursor.execute("""INSERT OR REPLACE into multiple_charged_ions (peak_id_a, peak_id_b, label_a, label_b, charge_a, charge_b, ppm_error)
                           values (?,?,?,?,?,?,?)""", (assignment["peak_id_a"], assignment["peak_id_b"],
                                                       assignment["label_a"], assignment["label_b"], assignment["charge_a"], assignment["charge_b"], assignment["ppm_error"]))
    conn.commit()
    conn.close()
    return


def annotate_molecular_formulae(peaklist, lib_adducts, ppm, db_out, db_in="http://mfdb.bham.ac.uk", rules=True, max_mz=None):

    conn = sqlite3.connect(db_out)
    cursor = conn.cursor()

    cursor.execute("DROP TABLE IF EXISTS molecular_formulae")

    cursor.execute("""CREATE TABLE molecular_formulae (
                    id TEXT DEFAULT NULL,
                    mz REAL DEFAULT NULL,
                    exact_mass REAL DEFAULT NULL,
                    ppm_error REAL DEFAULT NULL,
                    adduct TEXT DEFAULT NULL,
                    C INTEGER DEFAULT 0,
                    H INTEGER DEFAULT 0,
                    N INTEGER DEFAULT 0,
                    O INTEGER DEFAULT 0,
                    P INTEGER DEFAULT 0,
                    S INTEGER DEFAULT 0,
                    CHNOPS INTEGER DEFAULT NULL,
                    molecular_formula TEXT DEFAULT NULL,
                    HC INTEGER DEFAULT NULL,
                    NOPSC INTEGER DEFAULT NULL,
                    lewis INTEGER DEFAULT NULL,
                    senior INTEGER DEFAULT NULL,
                    double_bond_equivalents REAL DEFAULT NULL,
                    primary key (id, mz, molecular_formula, adduct)
                    );""")

    if os.path.isfile(db_in):
        conn_mem = DbMolecularFormulaeMemory(db_in)
        max_mz = None
    else:
        url = '{}/api/formula/mass_range'.format(db_in)
        url_test = '{}/api/formula/mass?mass=180.06339&tol=0.0&tol_unit=ppm&rules=1'.format(db_in)
        o = urlparse(url)
        if o.scheme != "http" and o.netloc != "mfdb.bham.ac.uk":
            raise ValueError("No database or local db available")
        else:
            r = requests.get(url_test)
            r.raise_for_status()

    path_nist_database = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'nist_database.txt')
    nist_database = nist_database_to_pyteomics(path_nist_database)

    for i in range(len(peaklist.iloc[:, 0])):
        mz = float(peaklist["mz"].iloc[i])
        name = str(peaklist["name"].iloc[i])

        min_tol, max_tol = calculate_mz_tolerance(mz, ppm)

        if max_mz is not None and mz > max_mz:  # TODO
            continue

        values = []
        for adduct in lib_adducts.lib:

            if mz - lib_adducts.lib[adduct] > 0.5:

                if "conn_mem" in locals():
                    records = conn_mem.select_mf(min_tol - lib_adducts.lib[adduct], max_tol - lib_adducts.lib[adduct], rules)
                else:
                    params = {"lower": min_tol - lib_adducts.lib[adduct],
                              "upper": max_tol - lib_adducts.lib[adduct],
                              "rules": int(rules)}
                    response = requests.get(url, params=params)
                    records = response.json()["records"]

                for record in records:
                    record["id"] = name
                    if "CHNOPS" not in record:  # MFdb API specific
                        record["CHNOPS"] = True  # MFdb API specific
                    if "rules" in record:
                        record.update(record["rules"])
                        del record["rules"]
                    if "atoms" in record:
                        record.update(record["atoms"])
                        del record["atoms"]
                    record["exact_mass"] = record["exact_mass"] + lib_adducts.lib[adduct]
                    record["mz"] = mz
                    record["ppm_error"] = calculate_ppm_error(mz, record["exact_mass"])
                    comp = OrderedDict([(item, record[item]) for item in record if item in nist_database.keys()])
                    record["molecular_formula"] = composition_to_string(comp)
                    record["adduct"] = adduct
                records = _remove_elements_from_compositions(records, keep=["C", "H", "N", "O", "P", "S"])
                values.extend([list(record.values()) for record in records])

        time.sleep(0.02)
        if len(values) > 0:
            cursor.executemany("""insert into molecular_formulae ({}) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                               """.format(",".join(map(str, list(record.keys())))), values)
    conn.commit()
    conn.close()
    return


def annotate_compounds(peaklist, lib_adducts, ppm, db_out, db_name, db_in=""):

    if db_in is None or db_in == "":
        path_dbs = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'databases')
        conn_local = None
        for db_local in os.listdir(path_dbs):
            if db_name == db_local.replace(".sql.gz", ""):

                with gzip.GzipFile(os.path.join(path_dbs, db_local), mode='rb') as db_dump:

                    conn_local = sqlite3.connect(":memory:")
                    cursor_local = conn_local.cursor()
                    cursor_local.executescript(db_dump.read().decode('utf-8'))
                    conn_local.commit()

                    cursor_local.execute("CREATE INDEX idx_exact_mass ON {} (exact_mass)".format(db_name.replace(".sql.gz", "")))

                    cursor_local.execute("SELECT name FROM sqlite_master WHERE type='table'")
                    if (db_name.replace(".sql.gz", ""), ) not in cursor_local.fetchall():
                        raise ValueError("Database {} not available".format(db_name))
                    break

        if conn_local is None:
            raise ValueError("Database {} not available".format(db_name))

    elif os.path.isfile(db_in):
        with open(db_in, 'rb') as fd:
            if fd.read(100)[:16].decode() == 'SQLite format 3\x00':
                conn_local = sqlite3.connect(db_in)
                cursor_local = conn_local.cursor()
                cursor_local.execute("SELECT name FROM sqlite_master WHERE type='table'")
                if not (db_name, ) in cursor_local.fetchall():
                    raise ValueError("Database {} not available".format(db_name))
            else:
                conn_mem = DbCompoundsMemory(db_in)
    else:
        raise IOError("[Errno 2] No such file or directory: {}".format(db_in))

    conn = sqlite3.connect(db_out)
    cursor = conn.cursor()

    cursor.execute("DROP TABLE IF EXISTS compounds_{}".format(db_name))
    cursor.execute("""CREATE TABLE compounds_{} (
                   id TEXT DEFAULT NULL,
                   mz REAL DEFAULT NULL,
                   exact_mass REAL DEFAULT NULL,
                   ppm_error REAL DEFAULT NULL,
                   adduct TEXT DEFAULT NULL,
                   C INTEGER DEFAULT 0,
                   H INTEGER DEFAULT 0,
                   N INTEGER DEFAULT 0,
                   O INTEGER DEFAULT 0,
                   P INTEGER DEFAULT 0,
                   S INTEGER DEFAULT 0,
                   CHNOPS INTEGER DEFAULT NULL,
                   molecular_formula TEXT DEFAULT NULL,
                   compound_id TEXT DEFAULT NULL,
                   compound_name TEXT DEFAULT NULL,
                   primary key (id, compound_id, adduct)
                   );""".format(db_name))

    for i in range(len(peaklist.iloc[:, 0])):
        mz = float(peaklist["mz"].iloc[i])
        name = str(peaklist["name"].iloc[i])
        min_tol, max_tol = calculate_mz_tolerance(mz, ppm)

        for adduct in lib_adducts.lib:

            if mz - lib_adducts.lib[adduct] > 0.5:

                if "conn_mem" in locals():
                    records = conn_mem.select_compounds(min_tol - lib_adducts.lib[adduct], max_tol - lib_adducts.lib[adduct])
                elif "conn_local" in locals():
                    col_names = ["compound_id", "C", "H", "N", "O", "P", "S", "CHNOPS", "molecular_formula", "compound_name", "exact_mass"]
                    cursor_local.execute("""SELECT id, C, H, N, O, P, S, CHNOPS,
                                            molecular_formula, name, exact_mass
                                            from {} where exact_mass >= {} and exact_mass <= {}
                                            """.format(db_name, min_tol - lib_adducts.lib[adduct], max_tol - lib_adducts.lib[adduct]))
                    records = [OrderedDict(zip(col_names, list(record))) for record in cursor_local.fetchall()]

                for record in records:
                    record["id"] = name
                    record["exact_mass"] = record["exact_mass"] + float(lib_adducts.lib[adduct])
                    record["mz"] = mz
                    record["ppm_error"] = calculate_ppm_error(mz, record["exact_mass"])
                    record["adduct"] = adduct
                    cursor.execute("""insert into compounds_{} ({}) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                                   """.format(db_name, ",".join(map(str, list(record.keys())))), list(record.values()))
    conn.commit()
    conn.close()
    return


def predict_drug_products(smiles, phase1_cycles, phase2_cycles):

    try:
        from rdkit import Chem
        import sygma
    except ImportError:
        raise ImportError('Install RDKit and/or SyGMa')

    # sygma/rules/phase1.txt
    # sygma/rules/phase2.txt

    # Each step in a scenario lists the ruleset and the number of reaction cycles to be applied
    scenario = sygma.Scenario([
        [sygma.ruleset['phase1'], phase1_cycles],
        [sygma.ruleset['phase2'], phase2_cycles]])

    # An rdkit molecule, optionally with 2D coordinates, is required as parent molecule
    parent = Chem.MolFromSmiles(smiles)

    metabolic_tree = scenario.run(parent)
    metabolic_tree.calc_scores()
    return metabolic_tree


class DbDrugCompoundsMemory:

    def __init__(self):
        self.conn = sqlite3.connect(":memory:")
        self.cursor = self.conn.cursor()
        self.cursor.execute("""CREATE TABLE predicted_drug_products (
                            compound_id TEXT PRIMARY KEY  NOT NULL,
                            compound_name TEXT,
                            smiles TEXT,
                            exact_mass decimal(15,7),
                            C INTEGER DEFAULT 0,
                            H INTEGER DEFAULT 0,
                            N INTEGER DEFAULT 0,
                            O INTEGER DEFAULT 0,
                            P INTEGER DEFAULT 0,
                            S INTEGER DEFAULT 0,
                            CHNOPS INTEGER DEFAULT NULL,
                            molecular_formula TEXT DEFAULT NULL,
                            sygma_score decimal(15,7),
                            sygma_pathway TEXT,
                            parent TEXT
                            );""")

        self.cursor.execute("""CREATE INDEX IDX_EXACT_MASS ON predicted_drug_products (exact_mass);""")
        self.conn.commit()

    def insert(self, records):
        for record in records:
            columns = ",".join(map(str, list(record.keys())))
            qms = ', '.join(['?'] * len(record.values()))
            query = """insert into predicted_drug_products ({}) values ({})""".format(columns, qms)
            self.cursor.execute(query, list(record.values()))
        self.conn.commit()

    def select(self, min_tol, max_tol):
        col_names = ["compound_id", "compound_name", "smiles", "sygma_score", "sygma_pathway", "parent", "exact_mass", "C", "H", "N", "O", "P", "S", "CHNOPS", "molecular_formula"]
        self.cursor.execute("""SELECT {} FROM predicted_drug_products WHERE 
                            exact_mass >= {} and exact_mass <= {}
                            """.format(",".join(map(str, col_names)), min_tol, max_tol))
        return [OrderedDict(zip(col_names, list(record))) for record in self.cursor.fetchall()]

    def close(self):
        self.conn.close()


def annotate_drug_products(peaklist, db_out, list_smiles, lib_adducts, ppm, phase1_cycles, phase2_cycles):

    try:
        from rdkit import Chem
        import sygma
    except ImportError:
        raise ImportError('Install RDKit and/or SyGMa')

    conn = sqlite3.connect(db_out)
    cursor = conn.cursor()

    cursor.execute("DROP TABLE IF EXISTS drug_products")
    cursor.execute("""CREATE TABLE drug_products (
                   id TEXT DEFAULT NULL,
                   mz REAL DEFAULT NULL,
                   exact_mass REAL DEFAULT NULL,
                   ppm_error REAL DEFAULT NULL,
                   adduct TEXT DEFAULT NULL,
                   C INTEGER DEFAULT 0,
                   H INTEGER DEFAULT 0,
                   N INTEGER DEFAULT 0,
                   O INTEGER DEFAULT 0,
                   P INTEGER DEFAULT 0,
                   S INTEGER DEFAULT 0,
                   CHNOPS INTEGER DEFAULT NULL,
                   molecular_formula TEXT DEFAULT NULL,
                   compound_id TEXT DEFAULT NULL,
                   compound_name TEXT DEFAULT NULL,
                   smiles TEXT,
                   sygma_score REAL DEFAULT 0.0,
                   sygma_pathway TEXT,
                   parent TEXT,
                   primary key (id, adduct, compound_id)
                   );""")

    path_nist_database = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'nist_database.txt')
    nist_database = nist_database_to_pyteomics(path_nist_database)

    records = []
    for smiles_parent in list_smiles:
        metabolic_tree = predict_drug_products(smiles_parent, phase1_cycles, phase2_cycles)
        for entry in metabolic_tree.to_list():
            smiles_product = Chem.MolToSmiles(entry['SyGMa_metabolite'])
            record = OrderedDict()
            record["compound_id"] = smiles_product
            record["compound_name"] = smiles_product
            record["sygma_pathway"] = entry["SyGMa_pathway"]
            record["parent"] = Chem.MolToSmiles(entry["parent"])
            mf = Chem.rdMolDescriptors.CalcMolFormula(Chem.MolFromSmiles(smiles_product))
            record["smiles"] = smiles_product
            record["sygma_score"] = entry['SyGMa_score']
            comp = pyteomics_mass.Composition(mf)
            record.update(comp)
            record["molecular_formula"] = composition_to_string(comp)
            record["exact_mass"] = round(pyteomics_mass.calculate_mass(formula=str(mf), mass_data=nist_database), 6)
            record["CHNOPS"] = sum([comp[e] for e in comp if e in ["C", "H", "N", "O", "P", "S"]]) == sum(list(comp.values()))
            records.append(record)

    conn_mem = DbDrugCompoundsMemory()
    records = _remove_elements_from_compositions(records, keep=["C", "H", "N", "O", "P", "S"])
    conn_mem.insert(records)

    for i in range(len(peaklist.iloc[:, 0])):
        mz = float(peaklist["mz"].iloc[i])
        name = str(peaklist["name"].iloc[i])
        min_tol, max_tol = calculate_mz_tolerance(mz, ppm)
        for adduct in lib_adducts.lib:

            if mz - lib_adducts.lib[adduct] > 0.5:

                records = conn_mem.select(min_tol - lib_adducts.lib[adduct], max_tol - lib_adducts.lib[adduct])

                for record in records:
                    record["id"] = name
                    record["exact_mass"] = record["exact_mass"] + float(lib_adducts.lib[adduct])
                    record["mz"] = mz
                    record["ppm_error"] = calculate_ppm_error(mz, record["exact_mass"])
                    record["adduct"] = adduct
                    cursor.execute("""insert into drug_products ({}) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                                   """.format(",".join(map(str, list(record.keys())))), list(record.values()))
    conn.commit()
    conn.close()
    return


def summary(df, db, single_row=False, single_column=False, convert_rt=None, ndigits_mz=None):

    conn = sqlite3.connect(db)
    cursor = conn.cursor()

    cursor.execute("DROP TABLE IF EXISTS peaklist")
    df[["name", "mz", "rt", "intensity"]].to_sql("peaklist", conn, index=False)

    cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = cursor.fetchall()

    tables_amo = ["adduct_pairs", "multiple_charged_ions", "oligomers", "isotopes"]
    tables_to_union = []
    for tn in tables:
        if tn[0] in tables_amo:
            tables_to_union.append(str(tn[0]))

    if len(tables_to_union) > 0 and ("groups",) in tables:

        if len(tables_to_union) > 1:
            query = "select peak_id_a, peak_id_b from "
            query += " union select peak_id_a, peak_id_b from ".join(map(str, tables_to_union))
        elif len(tables_to_union) == 1:
            query = "select peak_id_a, peak_id_b from {}".format(tables_to_union[0])
        cursor.execute(query)

        records = [(str(record[0]), str(record[1])) for record in cursor.fetchall()]

        G = nx.OrderedDiGraph()
        G.add_edges_from(records)

        graphs = list(G.subgraph(c) for c in nx.weakly_connected_components(G))

        to_add = []
        for i, g in enumerate(graphs):
            for n in g.nodes():
                to_add.append([i+1, n, g.degree(n), g.number_of_nodes(), g.number_of_edges()])

        cursor.execute("""CREATE TEMPORARY TABLE sub_groups (
                           sub_group_id INTEGER DEFAULT NULL,
                           peak_id INTEGER DEFAULT NULL,
                           degree INTEGER DEFAULT NULL,             
                           n_nodes INTEGER DEFAULT NULL,
                           n_edges INTEGER DEFAULT NULL,
                           PRIMARY KEY (sub_group_id, peak_id));""")

        cursor.executemany("""insert into sub_groups (sub_group_id, peak_id, degree, n_nodes, n_edges) 
                           values (?,?,?,?,?)""", to_add)

        columns_groupings = """peak_id, group_id, degree_cor, sub_group_id, degree, n_nodes, n_edges"""

        query_groupings = """select distinct gr.peak_id as peak_id, gr.group_id as group_id, degree_cor,
                          sub_groups.sub_group_id as sub_group_id, sub_groups.degree as degree,
                          sub_groups.n_nodes as n_nodes, sub_groups.n_edges as n_edges
                          from (select group_id, peak_id_a as peak_id, degree_a as degree_cor from groups
                          union
                          select group_id, peak_id_b as peak_id, degree_b as degree_cor from groups) AS gr
                          LEFT JOIN sub_groups
                          ON gr.peak_id = sub_groups.peak_id"""
    else:
        query_groupings = ""
        columns_groupings = ""

    flag_amo = len([tl for tl in tables_amo if (tl,) in tables]) > 0
    flag_isotopes = ("isotopes",) in tables

    if flag_amo:
        sub_queries = []
        for tl in tables_amo:
            if (tl,) in tables:
                if tl == "adduct_pairs":
                    sub_queries.append("""select peak_id_a as peak_id_amo, label_a as label, 1 as charge, 1 as oligomer from adduct_pairs
                    union
                    select peak_id_b as peak_id_amo, label_b as label, 1 as charge, 1 as oligomer from adduct_pairs""")
                elif tl == "multiple_charged_ions":
                    sub_queries.append("""select peak_id_a as peak_id_amo, label_a as label, charge_a as charge, 1 as oligomer from multiple_charged_ions
                    union
                    select peak_id_b as peak_id_amo, label_b as label, charge_b as charge, 1 as oligomer from multiple_charged_ions""")
                elif tl == "oligomers":
                    sub_queries.append("""select peak_id_a as peak_id_amo, label_a as label, 1 as charge, 1 as oligomer from oligomers
                    union
                    select peak_id_b as peak_id_amo, label_b as label, 1 as charge, round(mz_ratio) as oligomer from oligomers""")
        columns_amo = ", label, charge, oligomer"
        query_amo = " union ".join(map(str, sub_queries))

    if flag_isotopes:
        columns_isotopes = ", isotope_labels_a, isotope_ids, isotope_labels_b, atoms"
        query_isotopes = """SELECT peak_id_a, group_concat(label_a) as isotope_labels_a,
                            group_concat(peak_id_b, ",") as isotope_ids,
                            group_concat(label_b) as isotope_labels_b, group_concat(round(atoms,1), ",") as atoms
                            from (select peak_id_a, label_a, peak_id_b, label_b, atoms, ppm_error from isotopes
                            union
                            select peak_id_b as peak_id_a, label_b as label_a,
                            peak_id_a as peak_id_b, label_a as label_b, atoms, ppm_error
                            from isotopes
                            ) group by peak_id_a"""

    cursor.execute("DROP TABLE IF EXISTS peak_labels")
    if flag_amo and flag_isotopes:
        query = "CREATE TABLE peak_labels as "
        if query_groupings != "":
            query += "SELECT {}{}{} from """.format(columns_groupings, columns_amo, columns_isotopes)
            query += "({}) LEFT JOIN ({}) ON peak_id = peak_id_amo LEFT JOIN ({}) ON peak_id = peak_id_a".format(query_groupings, query_amo, query_isotopes)
        else:
            query += "SELECT peaklist.name as peak_id{}{} from ".format(columns_amo, columns_isotopes)
            query += "peaklist LEFT JOIN ({}) ON peaklist.name = peak_id LEFT JOIN ({}) ON peaklist.name = peak_id_a".format(query_amo, query_isotopes)
            query = query.replace("peak_id_amo", "peak_id")
        cursor.execute(query)
    elif flag_isotopes and not flag_amo:
        query = "CREATE TABLE peak_labels as "
        if query_groupings != "":
            query += "select {}{} from ".format(columns_groupings, columns_isotopes)
            query += "({}) LEFT JOIN ({}) ON peak_id = peak_id_a".format(query_groupings, query_isotopes)
        else:
            query += query_isotopes
        cursor.execute(query)
    elif not flag_isotopes and flag_amo:
        query = "CREATE TABLE peak_labels as "
        if query_groupings != "":
            query += """select {}{} from """.format(columns_groupings, columns_amo)
            query += """({}) LEFT JOIN ({}) ON peak_id = peak_id_amo""".format(query_groupings, query_amo)
        else:
            query += query_amo.replace("peak_id_amo", "peak_id")
        cursor.execute(query)
    if flag_amo:
        cursor.execute('PRAGMA table_info("peak_labels")')
        columns = cursor.fetchall()

        set_to_NULL = ["label", "charge", "oligomer"]
        columns_to_select = []
        for cn in columns:
            if cn[1] in set_to_NULL:
                columns_to_select.append("NULL")
            else:
                columns_to_select.append(cn[1])

        query = "INSERT INTO peak_labels"
        query += " SELECT {} FROM peak_labels where label is not NULL".format(", ".join(map(str, columns_to_select)))

        cursor.execute(query)
        conn.commit()

    cpd_tables = [tn[0] for tn in tables if "compound" in tn[0]]

    flag_mf = ("molecular_formulae",) in tables
    flag_cpd = len(cpd_tables) > 0

    columns = ["exact_mass", "ppm_error", "adduct", "C", "H", "N", "O", "P", "S", "molecular_formula"]

    if len(cpd_tables) > 1:
        unions_cpd_sub_query = "LEFT JOIN (select * from "
        unions_cpd_sub_query += " union select * from ".join(map(str, cpd_tables))
        unions_cpd_sub_query += ") as ct "
    elif len(cpd_tables) == 1:
        unions_cpd_sub_query = "LEFT JOIN (select * from {}) as ct".format(cpd_tables[0])
    else:
        unions_cpd_sub_query = ""

    if flag_mf and flag_cpd:

        unions_cpd_query = "CREATE TEMP TABLE compounds AS select * from "
        unions_cpd_query += " union select * from ".join(map(str, cpd_tables))

        cursor.execute(unions_cpd_query)
        unions_cpd_sub_query = ""

        query = """CREATE TEMP TABLE mf_cd as
                   SELECT mf.id, mf.exact_mass, mf.ppm_error, mf.adduct, mf.C, mf.H, mf.N, mf.O, mf.P, mf.S,
                   mf.molecular_formula, cpds.compound_name, cpds.compound_id
                   FROM molecular_formulae as mf
                   LEFT JOIN compounds as cpds
                   ON mf.molecular_formula = cpds.molecular_formula AND mf.adduct = cpds.adduct
                   UNION
                   SELECT cpds.id, cpds.exact_mass, cpds.ppm_error, cpds.adduct, cpds.C, cpds.H, cpds.N, cpds.O, cpds.P, cpds.S,
                   cpds.molecular_formula, cpds.compound_name, cpds.compound_id
                   FROM compounds as cpds
                   LEFT JOIN molecular_formulae as mf
                   ON mf.molecular_formula = cpds.molecular_formula AND mf.adduct = cpds.adduct
                   WHERE mf.molecular_formula IS NULL"""

        cursor.execute(query)

        # mf_cpc_columns = "".join(map(str, [", mf.{} as {}".format(c, c) for c in columns]))
        # mf_cpc_columns += ", ct.compound_name as compound_name, ct.compound_id as compound_id"
        # unions_cpd_sub_query += " ON mf.molecular_formula = ct.molecular_formula AND mf.adduct = ct.adduct"
        # if flag_amo:
        #    union_mf_sub_query = "LEFT JOIN molecular_formulae AS mf ON (peaklist.name = mf.id and peak_labels.label = mf.adduct)"
        #     union_mf_sub_query += " OR (peaklist.name = mf.id AND peak_labels.label is NULL and not exists (select 1 from peak_labels where peak_id = mf.id and label = mf.adduct))"
        # else:
        #     union_mf_sub_query = "LEFT JOIN molecular_formulae AS mf ON peaklist.name = mf.id"

        mf_cpc_columns = "".join(map(str, [", mf_cd.{} as {}".format(c, c) for c in columns]))
        mf_cpc_columns += ", mf_cd.compound_name as compound_name, mf_cd.compound_id as compound_id"
        if flag_amo:
            union_mf_sub_query = "LEFT JOIN mf_cd ON (peaklist.name = mf_cd.id and peak_labels.label = mf_cd.adduct)"
            union_mf_sub_query += " OR (peaklist.name = mf_cd.id AND peak_labels.label is NULL and not exists (select 1 from peak_labels where peak_id = mf_cd.id and label = mf_cd.adduct))"
        else:
            union_mf_sub_query = "LEFT JOIN mf_cd ON peaklist.name = mf_cd.id"

    elif not flag_mf and flag_cpd:
        mf_cpc_columns = "".join(map(str,[", ct.{} as {}".format(c, c) for c in columns]))
        mf_cpc_columns += ", compound_name as compound_name, compound_id as compound_id"
        if flag_amo:
            unions_cpd_sub_query += " ON (peaklist.name = ct.id AND peak_labels.label = adduct)"
            unions_cpd_sub_query += " OR (peaklist.name = ct.id AND peak_labels.label is NULL and not exists (select 1 from peak_labels where peak_id = ct.id and label = ct.adduct))"
        else:
            unions_cpd_sub_query += " ON peaklist.name = ct.id"
        union_mf_sub_query = ""

    elif flag_mf and not flag_cpd:
        mf_cpc_columns = "".join(map(str, [", mf.{} as {}".format(c, c) for c in columns]))
        if flag_amo:
            union_mf_sub_query = "LEFT JOIN molecular_formulae AS mf"
            union_mf_sub_query += " ON (peaklist.name = mf.id AND peak_labels.label = mf.adduct)"
            union_mf_sub_query += " OR (peaklist.name = mf.id AND peak_labels.label is NULL and not exists (select 1 from peak_labels where peak_id = mf.id and label = mf.adduct))"
        else:
            union_mf_sub_query = "LEFT JOIN molecular_formulae AS mf"
            union_mf_sub_query += " ON peaklist.name = mf.id"
    else:
        mf_cpc_columns = ""
        union_mf_sub_query = ""

    cursor.execute('PRAGMA table_info("peak_labels")')
    columns_peak_labels = cursor.fetchall()

    if len(columns_peak_labels) == 0 and mf_cpc_columns == "":
        raise ValueError("No annotation results available to create summary from")

    exclude_cns = ["peak_id"]

    if len(columns_peak_labels) > 0:
        pl_columns = ", " + ", ".join(map(str, ["peak_labels.{}".format(cn[1]) for cn in columns_peak_labels if cn[1] not in exclude_cns]))
        join_peak_labels = """
                           LEFT JOIN
                           peak_labels
                           ON peaklist.name = peak_labels.peak_id
                           """
    else:
        pl_columns = ""
        join_peak_labels = ""

    query = """CREATE TABLE summary AS SELECT
               peaklist.name, peaklist.mz, peaklist.rt, peaklist.intensity{}{}
               FROM peaklist
               {}
               {}
               {}
               """.format(pl_columns, mf_cpc_columns, join_peak_labels, union_mf_sub_query, unions_cpd_sub_query)
               # ORDER BY peaklist.rt, peaklist.mz

    cursor.execute("DROP TABLE IF EXISTS summary")
    cursor.execute(query)
    conn.commit()

    columns_to_select = []
    if ("groups",) in tables:
        columns_to_select.append("group_id, degree_cor, sub_group_id, degree, n_nodes, n_edges")
    if ("adduct_pairs",) in tables or ("oligomers",) in tables or ("multiple_charged_ions",) in tables:
        columns_to_select.append("""(select group_concat(label || '::' || charge || '::' || oligomer, '||')
        from (select distinct label, charge, oligomer from summary as s where summary.name = s.name)
        ) as label_charge_oligomer""")
    if ("isotopes",) in tables:
        columns_to_select.append("isotope_labels_a, isotope_ids, isotope_labels_b, atoms")

    if single_row:

        if flag_cpd:
            if single_column:
                columns_to_select.append("""
                             group_concat(
                                 molecular_formula || '::' || adduct || '::' || ifnull(compound_name, "None") || '::' || ifnull(compound_id, "None")  || '::' || exact_mass || '::' || round(ppm_error, 2) ,
                                 '||'
                             ) as annotation
                             """)
            else:
                columns_to_select.append("""
                             group_concat(molecular_formula, '||') as molecular_formula,
                             group_concat(adduct, '||') as adduct, 
                             group_concat(ifnull(compound_name, "None"), '||') as compound_name, 
                             group_concat(ifnull(compound_id, "None"), '||') as compound_id,
                             group_concat(exact_mass, '||') as exact_mass,
                             group_concat(round(ppm_error, 2), '||') as ppm_error
                             """)
        elif flag_mf:
            if single_column:
                columns_to_select.append("""
                             group_concat(
                                 molecular_formula || '::' || adduct || '::' || exact_mass || '::' || round(ppm_error, 2) ,
                                 '||'
                             ) as annotation
                             """)
            else:
                columns_to_select.append("""
                             group_concat(molecular_formula, '||') as molecular_formula, 
                             group_concat(adduct, '||') as adduct,
                             group_concat(exact_mass, '||') as exact_mass,
                             group_concat(round(ppm_error, 2), '||') as ppm_error
                             """)

        query = """SELECT DISTINCT name, mz, rt, intensity, {}
                   from summary
                   GROUP BY NAME
                   ORDER BY rowid
                   """.format(", ".join(map(str, columns_to_select)))

        df_out = pd.read_sql(query, conn)
        df_out.columns = [name.replace("peaklist.", "").replace("peak_labels.", "") for name in list(df_out.columns.values)]

        if flag_cpd:
            if not single_column:
                df_out["compound_id"] = df_out["compound_id"].replace({"None": ""})
                df_out["compound_name"] = df_out["compound_name"].replace({"None": ""})
            else:
                df_out["annotation"] = df_out["annotation"].replace({"None": ""})
    else:
        df_out = pd.read_sql("select * from summary", conn)
        df_out.columns = [name.replace("peaklist.", "").replace("peak_labels.", "") for name in list(df_out.columns.values)]

    if convert_rt == "min" and "rt" in df_out.columns.values:
        rt_min = df_out["rt"] / 60.0
        df_out.insert(loc=df_out.columns.get_loc("rt")+1, column='rt_min', value=rt_min.round(2))
    elif convert_rt == "sec" and "rt" in df_out.columns.values:
        rt_sec = df_out["rt"] * 60.0
        df_out.insert(loc=df_out.columns.get_loc("rt")+1, column='rt_sec', value=rt_sec.round(1))
    elif convert_rt is not None:
        raise ValueError("Provide min, sec or None for convert_rt")

    if isinstance(ndigits_mz, int):
        df_out["mz"] = df_out["mz"].round(ndigits_mz)
    elif ndigits_mz is not None:
        raise ValueError("Provide integer or None for ndigits_mz")

    conn.close()
    return df_out
