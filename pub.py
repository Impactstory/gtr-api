from __future__ import unicode_literals
import os
import datetime
import shortuuid
import hashlib
import requests
from urllib import quote_plus
from collections import defaultdict
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy import sql

from app import db
from annotation_list import AnnotationList
from util import get_sql_answer
from util import run_sql

def call_dandelion(query_text_raw):
    if not query_text_raw:
        return None

    query_text = quote_plus(query_text_raw.encode('utf-8'), safe=':/'.encode('utf-8'))

    # if the query text is very short, don't autodetect the language, try it as english
    language = "auto"
    if len(query_text) < 40:
        language = "en"

    url_template = u"https://api.dandelion.eu/datatxt/nex/v1/?min_confidence=0.5&text={query}&lang={language}&country=-1&social=False&top_entities=8&include=image,abstract,types,categories,alternate_labels&token={key}"
    url = url_template.format(query=query_text, language=language, key=os.getenv("DANDELION_API_KEY"))
    # print url
    r = requests.get(url)
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

class PubType(db.Model):
    __tablename__ = "medline_article_publication_type"
    pmid = db.Column(db.Numeric, db.ForeignKey('medline_citation.pmid'), primary_key=True)
    publication_type = db.Column(db.Text, primary_key=True)

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

class Unpaywall(db.Model):
    __tablename__ = "bq_pubmed_doi_unpaywall_pmid_numeric_mv"
    doi = db.Column(db.Text)
    pmid = db.Column(db.Text, primary_key=True)
    pmid_numeric = db.Column(db.Numeric, db.ForeignKey('medline_citation.pmid'))
    pmcid = db.Column(db.Text)
    is_oa = db.Column(db.Boolean)
    best_host_type = db.Column(db.Text)
    best_version = db.Column(db.Text)
    oa_url = db.Column(db.Text)

class DoiLookup(db.Model):
    __tablename__ = "dois_pmid_lookup_pmid_numeric_mv"
    doi = db.Column(db.Text, primary_key=True)
    pmid_numeric = db.Column(db.Numeric, db.ForeignKey('medline_citation.pmid'))
    paperbuzz = db.relationship("Paperbuzz", uselist=False, lazy='subquery')

class Paperbuzz(db.Model):
    __tablename__ = "dois_with_ced_events"
    doi = db.Column(db.Text, db.ForeignKey(DoiLookup.doi), primary_key=True)
    num_events = db.Column(db.Numeric)


class Pub(db.Model):
    __tablename__ = "medline_citation"
    pmid = db.Column(db.Numeric, primary_key=True)
    article_title = db.Column(db.Text)
    journal_title = db.Column(db.Text)
    abstract_text = db.Column(db.Text)
    pub_date_year = db.Column(db.Text)
    date_of_electronic_publication = db.Column(db.Text)
    authors = db.relationship("Author", lazy='subquery')
    pub_other_ids = db.relationship("PubOtherId", lazy='subquery')
    pub_types = db.relationship("PubType", lazy='subquery')
    mesh = db.relationship("PubMesh", lazy='subquery')
    doi_lookup = db.relationship("DoiLookup", uselist=False, lazy='subquery')
    unpaywall_lookup = db.relationship("Unpaywall", uselist=False, lazy='subquery')

    @property
    def paperbuzz(self):
        if self.doi_lookup and self.doi_lookup.paperbuzz:
            return int(self.doi_lookup.paperbuzz.num_events)
        return 0

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
    def display_number_of_paperbuzz_events(self):
        return self.paperbuzz

    @property
    def display_is_oa(self):
        if self.unpaywall_lookup:
            return self.unpaywall_lookup.is_oa
        return None

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




    def call_dandelion_on_abstract(self):
        self.dandelion_abstract_annotation_list = None
        if self.abstract_text:
            dandelion_results = call_dandelion(self.abstract_text)
            self.dandelion_abstract_annotation_list = AnnotationList(dandelion_results)

    def call_dandelion_on_short_abstract(self):
        self.dandelion_short_abstract_annotation_list = None
        if self.short_abstract:
            dandelion_results = call_dandelion(self.short_abstract)
            self.dandelion_short_abstract_annotation_list = AnnotationList(dandelion_results)

    def call_dandelion_on_article_title(self):
        dandelion_results = call_dandelion(self.article_title)
        self.dandelion_title_annotation_list = AnnotationList(dandelion_results)

    @property
    def annotations_for_pictures(self):
        return self.dandelion_title_annotation_list.list()

    def set_annotation_distribution(self, annotation_distribution):
        for my_annotation in self.annotations_for_pictures:
            my_annotation.annotation_distribution = annotation_distribution


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
    def short_abstract(self):
        if not self.abstract_text:
            return self.abstract_text

        response = ""

        if "CONCLUSION:" in self.abstract_text:
            response = self.abstract_text.rsplit("CONCLUSION:", 1)[1]
        elif "CONCLUSIONS:" in self.abstract_text:
            response = self.abstract_text.rsplit("CONCLUSIONS:", 1)[1]
        else:
            response = "... "
            try:
                response += ". ".join(self.abstract_text.rsplit(". ", 3)[1:])
            except IndexError:
                response += self.abstract_text[-500:-1]

        response = response.strip()
        return response

    @property
    def adjusted_score(self):
        score = getattr(self, "score", 0)

        if self.journal_title and "cochrane database" in self.journal_title.lower():
            score += 10

        if self.display_pub_types:
            pub_type_pubmed = [p["pub_type_pubmed"] for p in self.display_pub_types]
            if "Consensus Development Conference" in pub_type_pubmed:
                score += 7
            if "Practice Guideline" in pub_type_pubmed:
                score += 7
            if "Guideline" in pub_type_pubmed:
                score += 7
            if "Review" in pub_type_pubmed:
                score += 3
            if "Meta-Analysis" in pub_type_pubmed:
                score += 3
            if "Randomized Controlled Trial" in pub_type_pubmed:
                score += 2
            if "Clinical Trial" in pub_type_pubmed:
                score += 1
            if "Comparative Study" in pub_type_pubmed:
                score += 0.5
            if "Case Reports" in pub_type_pubmed:
                score += -5
            if "English Abstract" in pub_type_pubmed:
                score += -5

        return score

    @property
    def display_pub_types(self):

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
            ['Case Reports', 'case study', 2],
            ['Comparative Study', 'research study', 2],
            ['Evaluation Studies', 'research study', 2],
            ['Validation Studies', 'research study', 2],
            ['Observational Study', 'research study', 2],
            ['Clinical Trial, Phase II', 'clinical trial', 2],
            ['Clinical Trial, Phase I', 'clinical trial', 2],
            ['Clinical Trial, Phase III', 'clinical trial', 2],
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

        response = []
        if not self.pub_types:
            return response

        for pub_type in self.pub_types:
            pub_type_name = pub_type.publication_type
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

    def to_dict_full(self):
        nerd_results = None
        # nerd_results = self.get_nerd()

        # commented out while we wait for a shorter, faster paperbuzz api result
        # paperbuzz_results = get_paperbuzz(self.display_doi)
        paperbuzz_results = None

        results = self.to_dict_serp()
        results["nerd"] = nerd_results
        results["paperbuzz"] = paperbuzz_results
        results["abstract"] = self.abstract_text

        return results

    def to_dict_serp(self):
        # dandelion_results = None
        # if hasattr(self, "dandelion_results"):
        #     dandelion_results = self.dandelion_results

        # dandelion_results = {
        #     "title": self.call_dandelion(self.article_title),
        #     "abstract": self.call_dandelion(self.abstract_text)
        # }

        # dandelion_title_annotation_list = self.call_dandelion(self.article_title)

        # if dandelion_results and dandelion_results.get("topEntities", None):
        #     dandelion_results = dandelion_results["topEntities"]
        # elif dandelion_results["annotations"]:
        #     dandelion_results = [d["uri"] for d in dandelion_results["annotations"]]

        response = {
            "pmid": self.pmid,
            "pmid_url": self.pmid_url,
            "doi": self.display_doi_url,
            "doi_url": self.display_doi_url,
            "title": self.article_title,
            "abstract": self.abstract_text,
            "short_abstract": self.short_abstract,
            "year": self.pub_date_year,
            "journal_name": self.journal_title,
            "date_of_electronic_publication": self.date_of_electronic_publication,
            "num_paperbuzz_events": self.display_number_of_paperbuzz_events,
            "author_lastnames": self.author_lastnames,
            "is_oa": self.display_is_oa,
            "oa_url": self.display_oa_url,
            "best_host": self.display_best_host,
            "best_version": self.display_best_version,
            "pub_types": self.display_pub_types,
            "mesh": [m.to_dict() for m in self.mesh],

            # "dandelion": dandelion_results,
            "image": {
                "url": "https://picsum.photos/300/200?random"
            },

            "snippet": getattr(self, "snippet", None),
            "score": self.adjusted_score
        }

        return response





def get_paperbuzz(doi):
    if not doi:
        return None

    print u"calling paperbuzz with {}".format(doi)
    data = None
    url = u"https://api.paperbuzz.org/v0/doi/{}?email=team+gtr@impactstory.org".format(doi)
    r = requests.get(url)
    if r.status_code == 200:
        data = r.json()
    return data
