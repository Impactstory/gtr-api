#!/usr/local/bin/python
# -*- coding: utf-8 -*-

from __future__ import unicode_literals
import os
import re
import datetime
import shortuuid
import hashlib
import requests
import random
import json
from urllib import quote_plus
from collections import defaultdict
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy import sql
from sqlalchemy import func
from sqlalchemy.orm import deferred
from sqlalchemy.ext.hybrid import hybrid_property, hybrid_method
from sqlalchemy.orm import column_property
from collections import OrderedDict
from collections import defaultdict

from app import db
from annotation_list import AnnotationList
from util import get_sql_answer
from util import run_sql
from util import TooManyRequestsException


pub_type_data = [
    ['Meta-Analysis', 'meta-analysis', 5],
    ['Systematic Review', 'review', 5],
    ['Practice Guideline', 'guidelines', 5],
    ['Guideline', 'guidelines', 5],
    ['Consensus Development Conference', 'review', 5],
    ['Patient Education Handout', 'guidelines', 5],
    ['Review', 'review', 4],
    ['Introductory Journal Article', 'review', 4],
    ['Randomized Controlled Trial', 'randomized controlled trial', 3.5],
    ['Clinical Trial', 'clinical trial', 3],
    ['Controlled Clinical Trial', 'clinical trial', 3],
    ['Comparative Study', 'research study', 2],
    ['Evaluation Studies', 'research study', 2],
    ['Validation Studies', 'research study', 2],
    ['Observational Study', 'research study', 2],
    ['Clinical Trial, Phase II', 'clinical trial', 2],
    ['Clinical Trial, Phase I', 'clinical trial', 2],
    ['Clinical Trial, Phase III', 'clinical trial', 2],
    ['Case Reports', 'case study', 1],
    ['Letter', 'editorial content', 1],
    ['Comment', 'editorial content', 1],
    ['Editorial', 'editorial content', 1],
    ['News', 'news and interest', 1],
    ['Biography', 'news and interest', 1],
    ['Published Erratum', 'editorial content', 1],
    ['Portraits', 'news and interest', 1],
    ['Interview', 'news and interest', 1],
    ['Newspaper Article', 'news and interest', 1],
    ['Retraction of Publication', 'editorial content', 1],
    ['Portrait', 'news and interest', 1],
    ['Autobiography', 'news and interest', 1],
    ['Personal Narratives', 'news and interest', 1],
    ['Retracted Publication', 'retracted', -1]]
pub_type_lookup = dict(zip([name for (name, label, val) in pub_type_data], pub_type_data))


def call_dandelion(query_text_raw, api_key=None, label_top_entities=True):
    # print "CALLING DANDELION"
    if not query_text_raw:
        return None

    if not api_key:
        api_key = os.getenv("DANDELION_API_KEY")

    query_text = quote_plus(query_text_raw.encode('utf-8'), safe=':/'.encode('utf-8'))

    # for right now assume everything is english, we get better results that way
    language = "en"

    url_template = u"https://api.dandelion.eu/datatxt/nex/v1/?min_confidence=0.5&text={query}&lang={language}&country=-1&social=False&include=image,abstract,types,categories,alternate_labels,lod&token={api_key}"
    if label_top_entities:
        url_template += u"&top_entities=8"
    url = url_template.format(query=query_text, language=language, api_key=api_key)
    r = requests.get(url)
    if r.headers.get("X-DL-units-left", None) == 0 or r.status_code == 401:
        print u"TooManyRequestsException"
        raise TooManyRequestsException

    try:
        response_data = r.json()
    except ValueError:
        response_data = None

    return response_data


class Author(db.Model):
    __tablename__ = "medline_author"
    pmid = db.Column(db.Numeric, db.ForeignKey('medline_citation.pmid'), primary_key=True)
    author_order = db.Column(db.Numeric, primary_key=True)
    last_name = db.Column(db.Text, primary_key=True)  # this one shouldn't have primary key once all orders are populated

class PubOtherId(db.Model):
    __tablename__ = "medline_citation_other_id"
    pmid = db.Column(db.Numeric, db.ForeignKey('medline_citation.pmid'), primary_key=True)
    source = db.Column(db.Text, primary_key=True)
    other_id = db.Column(db.Text)


class PubMesh(db.Model):
    __tablename__ = "medline_mesh_heading"
    pmid = db.Column(db.Numeric, db.ForeignKey('medline_citation.pmid'), primary_key=True)
    descriptor_name = db.Column(db.Text)
    descriptor_name_major_yn = db.Column(db.Text, primary_key=True)
    qualifier_name = db.Column(db.Text)
    qualifier_name_major_yn = db.Column(db.Text, primary_key=True)

    def to_dict(self):
        response = {}
        response["descriptor"] = self.descriptor_name
        if self.descriptor_name_major_yn == "Y":
            response["descriptor_is_major"] = True
        if self.qualifier_name and self.qualifier_name != "N/A":
            response["qualifier_name"] = self.qualifier_name.replace("&amp;", "and")
            if self.qualifier_name_major_yn == "Y":
                response["qualifier_is_major"] = True
        return response

class UnpaywallLookup(db.Model):
    __tablename__ = "ricks_unpaywall"
    doi = db.Column(db.Text, db.ForeignKey('ricks_gtr_sort_results.doi'), primary_key=True)
    is_oa = db.Column(db.Boolean)
    best_version = db.Column(db.Text)
    best_host_type = db.Column(db.Text)
    oa_url = db.Column(db.Text)
    published_date = db.Column(db.DateTime)

class News(db.Model):
    __tablename__ = "ricks_paperbuzz_news"
    doi = db.Column(db.Text, db.ForeignKey('ricks_gtr_sort_results.doi'), primary_key=True)
    event_id = db.Column(db.Text)
    news_url = db.Column(db.Text)
    news_title = db.Column(db.Text)
    occurred_at = db.Column(db.DateTime)

    def to_dict(self):
        response = {
            "news_url": self.news_url,
            "news_title": self.news_title,
            "occurred_at": self.occurred_at
        }
        return response


class Dandelion(db.Model):
    __tablename__ = "dandelion_by_doi"
    doi = db.Column(db.Text, db.ForeignKey('ricks_gtr_sort_results.doi'), primary_key=True)
    pmid = db.Column(db.Numeric)
    num_events = db.Column(db.Numeric)
    dandelion_collected = db.Column(db.DateTime)
    dandelion_raw_article_title = db.Column(JSONB)
    dandelion_raw_abstract_text = db.Column(db.Text)

    def __repr__(self):
        return u'<Dandelion ({doi}) {num_events} {dandelion_collected}>'.format(
            doi=self.doi,
            num_events=self.num_events,
            dandelion_collected=self.dandelion_collected
        )



class Pub(db.Model):
    __tablename__ = "medline_citation"
    pmid = db.Column(db.Numeric, db.ForeignKey('ricks_gtr_sort_results.pmid'), primary_key=True)
    journal_title = db.Column(db.Text)
    abstract_text = db.Column(db.Text)
    article_title = deferred(db.Column(db.Text), group="full")
    pub_date_year = deferred(db.Column(db.Text), group="full")
    authors = db.relationship("Author", lazy='subquery')
    # mesh = db.relationship("PubMesh", lazy='subquery')
    abstract_length = column_property(func.char_length(abstract_text))

    @property
    def pmid_url(self):
        return u"https://www.ncbi.nlm.nih.gov/pubmed/{}".format(self.pmid)

    @property
    def display_doi_url(self):
        if self.display_doi:
            return u"https://doi.org/{}".format(self.display_doi)
        return None

    @property
    def display_doi(self):
        if not self.doi_lookup:
            return None
        return self.doi_lookup.doi

    @property
    def sorted_authors(self):
        if not self.authors:
            return None
        authors = self.authors
        sorted_authors = sorted(authors, key=lambda x: x.author_order, reverse=False)
        return sorted_authors

    @property
    def author_lastnames(self):
        response = []
        if self.sorted_authors:
            response = [author.last_name for author in self.sorted_authors]
        return response


    @property
    def display_is_oa(self):
        if self.doi_lookup:
            return self.doi_lookup.is_oa
        return None

    @property
    def display_published_date(self):
        if self.doi_lookup:
            if self.doi_lookup.published_date:
                return self.doi_lookup.published_date.isoformat()[0:10]
        return None

    @property
    def display_oa_url(self):
        if self.doi_lookup:
            return self.doi_lookup.oa_url
        return None

    @property
    def display_best_host(self):
        if self.doi_lookup:
            return self.doi_lookup.best_host
        return None

    @property
    def display_best_version(self):
        if self.doi_lookup:
            return self.doi_lookup.best_version
        return None


    @property
    def dandelion_title(self):
        if self.dandelion_lookup:
            return self.dandelion_lookup.dandelion_raw_article_title
        return None

    @property
    def dandelion_abstract(self):
        if self.dandelion_lookup:
            return self.dandelion_lookup.dandelion_raw_abstract_text
        return None


    @property
    def dandelion_has_been_collected(self):
        if self.dandelion_lookup:
            if self.dandelion_lookup.dandelion_collected:
                return True
        return False

    @property
    def news_articles(self):
        if self.doi_lookup and self.doi_lookup.news:
            articles = sorted(self.doi_lookup.news, key=lambda x: x.occurred_at, reverse=True)
            return articles
        return []

    @property
    def dandelion_abstract_annotation_list(self):
        if hasattr(self, "fresh_dandelion_abstract_annotation_list"):
            return self.fresh_dandelion_abstract_annotation_list
        if self.dandelion_has_been_collected:
            if self.dandelion_lookup.dandelion_raw_abstract_text:
                dandelion_results = json.loads(self.dandelion_lookup.dandelion_raw_abstract_text)
                return AnnotationList(dandelion_results)
        return None

    @property
    def dandelion_title_annotation_list(self):
        if hasattr(self, "fresh_dandelion_article_annotation_list"):
            return self.fresh_dandelion_article_annotation_list
        if self.dandelion_has_been_collected:
            dandelion_results = self.dandelion_lookup.dandelion_raw_article_title
            return AnnotationList(dandelion_results)
        return None

    def call_dandelion_on_abstract(self):
        if not self.dandelion_has_been_collected:
            if self.abstract_text:
                dandelion_results = call_dandelion(self.abstract_text)
                self.fresh_dandelion_abstract_annotation_list = AnnotationList(dandelion_results)

    def call_dandelion_on_article_title(self):
        if not self.dandelion_has_been_collected:
            if self.article_title:
                dandelion_results = call_dandelion(self.article_title)
                self.fresh_dandelion_article_annotation_list = AnnotationList(dandelion_results)

    @property
    def annotations_for_pictures(self):
        try:
            return self.dandelion_title_annotation_list.list()
        except:
            return []

    @property
    def topics(self):
        try:
            topic_annotation_objects = sorted(self.dandelion_title_annotation_list.list(), key=lambda x: x.topic_score, reverse=True)
            response = [a.title for a in topic_annotation_objects if a.confidence > 0.7]
            # get rid of dups, but keep order
            response = list(OrderedDict.fromkeys(response))
        except:
            response = []
        return response


    def set_annotation_distribution(self, annotation_distribution):
        for my_annotation in self.annotations_for_pictures:
            my_annotation.annotation_distribution = annotation_distribution


    def abstract_with_annotations_dict(self, full=True):
        sections = []
        if self.abstract_structured:
            sections = self.abstract_structured
        elif self.abstract_text:
            background_text = ""
            summary_text = ""
            if "CONCLUSION:" in self.abstract_text:
                background_text = self.abstract_text.rsplit("CONCLUSION:", 1)[0]
                summary_text = self.abstract_text.rsplit("CONCLUSION:", 1)[1]
            elif "CONCLUSIONS:" in self.abstract_text:
                background_text = self.abstract_text.rsplit("CONCLUSIONS:", 1)[0]
                summary_text = self.abstract_text.rsplit("CONCLUSIONS:", 1)[1]
            else:
                try:
                    background_text += ". ".join(self.abstract_text.rsplit(". ", 3)[0:1]) + "."
                    summary_text += ". ".join(self.abstract_text.rsplit(". ", 3)[1:])
                except IndexError:
                    background_text += self.abstract_text[-500:-1]
                    summary_text += self.abstract_text[-500:-1]

            background_text = background_text.strip()
            summary_text = summary_text.strip()

            sections = [
                {"text": background_text, "heading": "BACKGROUND", "section_split_source": "automated", "summary": False, "original_start":1, "original_end":len(background_text)},
                {"text": summary_text, "heading": "SUMMARY", "section_split_source": "automated", "summary": True, "original_start":len(background_text)+2, "original_end":len(self.abstract_text)}
            ]

        if full:
            for section in sections:
                section["annotations"] = []
                if self.dandelion_abstract_annotation_list:
                    for anno in self.dandelion_abstract_annotation_list.list():
                        if anno.confidence >= 0.65:
                            if anno.start >= section["original_start"] and anno.end <= section["original_end"]:
                                my_anno_dict = anno.to_dict_simple()
                                my_anno_dict["start"] -= section["original_start"] - 1
                                my_anno_dict["end"] -= section["original_start"] - 1
                                section["annotations"] += [my_anno_dict]

        if not full:
            sections = [s for s in sections if s["summary"]==True]

        return sections


    def title_annotations_dict(self, full=True):
        response = []
        if full:
            if self.dandelion_title_annotation_list:
                response = self.dandelion_title_annotation_list.to_dict_simple()
        return response

    def get_nerd(self):
        if not self.abstract_text or len(self.abstract_text) <=3:
            return

        print u"calling nerd with {}".format(self.pmid)

        query_text = self.abstract_text
        query_text = query_text.replace("\n", " ")

        # url = u"http://cloud.science-miner.com/nerd/service/disambiguate"
        url = u"http://nerd.huma-num.fr/nerd/service/disambiguate"
        payload = {
            "text": query_text,
            "shortText": "",
            "termVector": [],
            "language": {
                "lang": "en"
            },
            "entities": [],
            "mentions": [
                "ner",
                "wikipedia"
            ],
            "nbest": False,
            "sentence": False,
            "customisation": "generic"
        }
        headers = {
            "Content-disposition": "form-data"
        }
        r = requests.post(url, json=payload)
        try:
            response_data = r.json()
        except ValueError:
            response_data = None
        return response_data


    @property
    def abstract_structured(self):
        all_sections = []

        working_text = self.abstract_text
        if working_text:

            # if &amp; in heading, need to replace with uppercase it won't match as heading
            working_text = working_text.replace("&amp;", " AND ")  # exactly the same length, so won't affect offsets
            if re.findall("(^[A-Z' ,&]{4,}): ", working_text):
                matches = re.findall(ur"([A-Z' ,&]{4,}): (.*?) (?=$|[A-Z' ,&]{4,}: )", working_text)
                for match in matches:
                    all_sections.append({
                        "heading": match[0],
                        "text": match[1]
                    })

        cursor = 1
        for section in all_sections:
            cursor += len(section["heading"])
            cursor += 2
            # don't include heading in what can be annotated
            section["original_start"] = cursor
            cursor += len(section["text"])
            section["original_end"] = cursor
            cursor += 1
            section["section_split_source"] = "structured"
            section["summary"] = False

        if all_sections:
            all_sections[-1]["summary"] = True

            # check it doesn't talk about funding or data.  if so, use previous heading instead.
            for heading_word in all_sections[-1]["heading"].split(" "):
                if heading_word in ["TRIAL", "DATA", "REGISTRATION", "FUNDING"]:
                    all_sections[-1]["summary"] = False
                    all_sections[-2]["summary"] = True
        return all_sections


    @property
    def score(self):
        if hasattr(self, "adjusted_score"):
            return self.adjusted_score
        return 0




class PubDoi(db.Model):
    __tablename__ = "ricks_gtr_sort_results"
    doi = db.Column(db.Text, primary_key=True)
    pmid = db.Column(db.Text)
    article_title = deferred(db.Column(db.Text), group="full")
    journal_title = deferred(db.Column(db.Text), group="full")
    is_oa = db.Column(db.Boolean)
    abstract_length = db.Column(db.Numeric)
    num_events = db.Column(db.Numeric)
    num_news_events = db.Column(db.Numeric)
    pub_types = db.Column(db.Text)
    genre = db.Column(db.Text)
    published_date = db.Column(db.DateTime)
    pubmed_lookup_list = db.relationship("Pub", lazy='dynamic')
    # pubmed_lookup = db.relationship("Pub", uselist=False, lazy='select')
    dandelion_lookup = db.relationship("Dandelion", uselist=False, lazy='subquery')
    unpaywall_lookup = db.relationship("UnpaywallLookup", uselist=False, lazy='subquery')
    news = db.relationship("News", lazy='subquery')

    @property
    def pubmed_lookup(self):
        if hasattr(self, "cached_pubmed_lookup"):
            return self.cached_pubmed_lookup

        self.cached_pubmed_lookup = None
        if self.pmid:
            hits = self.pubmed_lookup_list.all()
            if hits:
                self.cached_pubmed_lookup = hits[0]
        return self.cached_pubmed_lookup

    @property
    def display_doi(self):
        return self.doi

    @property
    def display_doi_url(self):
        return u"https://doi.org/{}".format(self.doi)

    @property
    def pmid_url(self):
        if self.pmid:
            return u"https://www.ncbi.nlm.nih.gov/pubmed/{}".format(self.pmid)
        return None

    @property
    def author_lastnames(self):
        if self.pubmed_lookup:
            return self.pubmed_lookup.author_lastnames
        return []

    @property
    def abstract_text(self):
        if self.pubmed_lookup:
            return self.pubmed_lookup.abstract_text
        return ""

    @property
    def display_oa_url(self):
        if self.unpaywall_lookup:
            return self.unpaywall_lookup.oa_url
        return None

    @property
    def display_best_host(self):
        if self.unpaywall_lookup:
            return self.unpaywall_lookup.best_host_type
        return None

    @property
    def display_best_version(self):
        if self.unpaywall_lookup:
            return self.unpaywall_lookup.best_version
        return None


    @property
    def suppress(self):
        if self.genre in ["dataset"]:
            return True

        if self.display_pub_types:
            pub_type_pubmed = [p["pub_type_pubmed"] for p in self.display_pub_types]
            if "Retracted Publication" in pub_type_pubmed:
                return True
        return False


    @property
    def score(self):
        if hasattr(self, "adjusted_score"):
            return self.adjusted_score
        return 0

    @property
    def display_pub_types(self):

        response = []
        if not self.pub_types:
            return response

        pub_types_list = self.pub_types.split(",")

        for pub_type_name in pub_types_list:
            if pub_type_name in pub_type_lookup:
                response.append({"pub_type_pubmed": pub_type_lookup[pub_type_name][0],
                                 "pub_type_gtr": pub_type_lookup[pub_type_name][1],
                                 "evidence_level": pub_type_lookup[pub_type_name][2]
                })
            else:
                include_it = True
                excludes = ["Journal Article", "Research Support"]
                for exclude_phrase in excludes:
                    if exclude_phrase in pub_type_name:
                        include_it = False
                if include_it:
                    response.append({"pub_type_pubmed": pub_type_name,
                                 "pub_type_gtr": None,
                                 "evidence_level": None})

        response = sorted(response, key=lambda x: x["evidence_level"] or 0, reverse=True)
        return response

    @property
    def display_year(self):
        try:
            if self.published_date:
                return int(self.published_date[0:4])
        except:
            pass
        return ""

    @property
    def display_article_title(self):
        try:
            if self.article_title:
                title = self.article_title
                title = re.sub(u"(<.?strong>)", "", title)
                title = re.sub(u"(<.?p>)", "", title)
                title = re.sub(u"(<.?em>)", "", title)
                title = title[0:500]
                return title
        except:
            pass
        return ""

    @property
    def annotations_for_pictures(self):
        try:
            return self.dandelion_title_annotation_list.list()
        except:
            return []

    def set_annotation_distribution(self, annotation_distribution):
        for my_annotation in self.annotations_for_pictures:
            my_annotation.annotation_distribution = annotation_distribution

    @property
    def topics(self):
        try:
            topic_annotation_objects = sorted(self.dandelion_title_annotation_list.list(), key=lambda x: x.topic_score, reverse=True)
            response = [a.title for a in topic_annotation_objects if a.confidence > 0.7]
            # get rid of dups, but keep order
            response = list(OrderedDict.fromkeys(response))
        except:
            response = []
        return response

    def title_annotations_dict(self, full=True):
        response = []
        if full:
            if self.dandelion_title_annotation_list:
                response = self.dandelion_title_annotation_list.to_dict_simple()
        return response

    @property
    def dandelion_title_annotation_list(self):
        if hasattr(self, "fresh_dandelion_article_annotation_list"):
            return self.fresh_dandelion_article_annotation_list
        if self.dandelion_has_been_collected:
            dandelion_results = self.dandelion_lookup.dandelion_raw_article_title
            return AnnotationList(dandelion_results)
        return None

    def call_dandelion_on_abstract(self):
        if not self.dandelion_has_been_collected:
            if self.abstract_text:
                dandelion_results = call_dandelion(self.abstract_text)
                self.fresh_dandelion_abstract_annotation_list = AnnotationList(dandelion_results)

    def call_dandelion_on_article_title(self):
        if not self.dandelion_has_been_collected:
            if self.article_title:
                dandelion_results = call_dandelion(self.article_title)
                self.fresh_dandelion_article_annotation_list = AnnotationList(dandelion_results)

    @property
    def dandelion_has_been_collected(self):
        if self.dandelion_lookup:
            if self.dandelion_lookup.dandelion_collected:
                return True
        return False

    @property
    def news_articles(self):
        if self.news:
            articles = sorted(self.news, key=lambda x: x.occurred_at, reverse=True)
            return articles
        return []

    def abstract_with_annotations_dict(self, full=True):
        sections = []
        if self.abstract_structured:
            sections = self.abstract_structured
        elif self.abstract_text:
            background_text = ""
            summary_text = ""
            if "CONCLUSION:" in self.abstract_text:
                background_text = self.abstract_text.rsplit("CONCLUSION:", 1)[0]
                summary_text = self.abstract_text.rsplit("CONCLUSION:", 1)[1]
            elif "CONCLUSIONS:" in self.abstract_text:
                background_text = self.abstract_text.rsplit("CONCLUSIONS:", 1)[0]
                summary_text = self.abstract_text.rsplit("CONCLUSIONS:", 1)[1]
            else:
                try:
                    background_text += ". ".join(self.abstract_text.rsplit(". ", 3)[0:1]) + "."
                    summary_text += ". ".join(self.abstract_text.rsplit(". ", 3)[1:])
                except IndexError:
                    background_text += self.abstract_text[-500:-1]
                    summary_text += self.abstract_text[-500:-1]

            background_text = background_text.strip()
            summary_text = summary_text.strip()

            sections = [
                {"text": background_text, "heading": "BACKGROUND", "section_split_source": "automated", "summary": False, "original_start":1, "original_end":len(background_text)},
                {"text": summary_text, "heading": "SUMMARY", "section_split_source": "automated", "summary": True, "original_start":len(background_text)+2, "original_end":len(self.abstract_text)}
            ]

        if full:
            for section in sections:
                section["annotations"] = []
                if self.dandelion_abstract_annotation_list:
                    for anno in self.dandelion_abstract_annotation_list.list():
                        if anno.confidence >= 0.65:
                            if anno.start >= section["original_start"] and anno.end <= section["original_end"]:
                                my_anno_dict = anno.to_dict_simple()
                                my_anno_dict["start"] -= section["original_start"] - 1
                                my_anno_dict["end"] -= section["original_start"] - 1
                                section["annotations"] += [my_anno_dict]

        if not full:
            sections = [s for s in sections if s["summary"]==True]

        return sections

    @property
    def abstract_structured(self):
        all_sections = []

        working_text = self.abstract_text
        if working_text:

            # if &amp; in heading, need to replace with uppercase it won't match as heading
            working_text = working_text.replace("&amp;", " AND ")  # exactly the same length, so won't affect offsets
            if re.findall("(^[A-Z' ,&]{4,}): ", working_text):
                matches = re.findall(ur"([A-Z' ,&]{4,}): (.*?) (?=$|[A-Z' ,&]{4,}: )", working_text)
                for match in matches:
                    all_sections.append({
                        "heading": match[0],
                        "text": match[1]
                    })

        cursor = 1
        for section in all_sections:
            cursor += len(section["heading"])
            cursor += 2
            # don't include heading in what can be annotated
            section["original_start"] = cursor
            cursor += len(section["text"])
            section["original_end"] = cursor
            cursor += 1
            section["section_split_source"] = "structured"
            section["summary"] = False

        if all_sections:
            all_sections[-1]["summary"] = True

            # check it doesn't talk about funding or data.  if so, use previous heading instead.
            for heading_word in all_sections[-1]["heading"].split(" "):
                if heading_word in ["TRIAL", "DATA", "REGISTRATION", "FUNDING"]:
                    all_sections[-1]["summary"] = False
                    all_sections[-2]["summary"] = True
        return all_sections

    @property
    def dandelion_abstract_annotation_list(self):
        if hasattr(self, "fresh_dandelion_abstract_annotation_list"):
            return self.fresh_dandelion_abstract_annotation_list
        if self.dandelion_has_been_collected:
            if self.dandelion_lookup.dandelion_raw_abstract_text:
                dandelion_results = json.loads(self.dandelion_lookup.dandelion_raw_abstract_text)
                return AnnotationList(dandelion_results)
        return None

    @property
    def dandelion_title_annotation_list(self):
        if hasattr(self, "fresh_dandelion_article_annotation_list"):
            return self.fresh_dandelion_article_annotation_list
        if self.dandelion_has_been_collected:
            dandelion_results = self.dandelion_lookup.dandelion_raw_article_title
            return AnnotationList(dandelion_results)
        return None



    def to_dict_serp(self, full=True):

        response = {
            "doi": self.doi,
            "doi_url": self.display_doi_url,
            "title": self.display_article_title,
            "year": self.display_year,
            "journal_name": self.journal_title,
            "num_paperbuzz_events": self.num_events,
            "is_oa": self.is_oa,
            "oa_url": self.display_oa_url,
            "oa_host": self.display_best_host,
            "oa_version": self.display_best_version,
            "published_date": self.published_date,
            "pub_types": self.display_pub_types,
            "genre": self.genre,
            # "snippet": getattr(self, "snippet", None),
            "score": self.score
        }

        if full:
            additional_items = {
            "pmid": self.pmid,
            "pmid_url": self.pmid_url,
            "author_lastnames": self.author_lastnames,
            # "mesh": [m.to_dict() for m in self.mesh],
            "news_articles": [] # [a.to_dict() for a in self.news_articles]
            }
            response.update(additional_items)

        return response


