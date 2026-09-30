"""Registry of 75+ top software houses and tech companies in Pakistan (Lahore, Islamabad, Faisalabad, and Remote)."""

from __future__ import annotations
from dataclasses import dataclass
from typing import Literal

AtsType = Literal["workable", "lever", "greenhouse", "recruitee", "custom", "firecrawl"]


@dataclass(frozen=True)
class CompanyEntry:
    id: str
    name: str
    cities: list[str]  # e.g. ["Lahore", "Islamabad", "Remote"]
    careers_url: str
    ats_type: AtsType = "custom"
    ats_identifier: str | None = None  # e.g. 'devsinc-17' for workable
    keywords: list[str] | None = None


# 75+ Curated Top Software Houses in Pakistan
TOP_PAKISTAN_TECH_COMPANIES: list[CompanyEntry] = [
    # Top Tier Multinational & Enterprise IT Exporters
    CompanyEntry(
        id="devsinc",
        name="Devsinc",
        cities=["Lahore", "Islamabad", "Remote"],
        careers_url="https://devsinc.com/careers",
        ats_type="workable",
        ats_identifier="devsinc-17",
        keywords=["ai", "python", "full stack", "react", "fastapi", "django", "node"],
    ),
    CompanyEntry(
        id="systemsltd",
        name="Systems Limited",
        cities=["Lahore", "Islamabad", "Karachi", "Remote"],
        careers_url="https://www.systemsltd.com/careers",
        ats_type="firecrawl",
        keywords=["software engineer", "developer", "cloud", "ai", "full stack", "dotnet"],
    ),
    CompanyEntry(
        id="netsol",
        name="NetSol Technologies",
        cities=["Lahore", "Remote"],
        careers_url="https://careers.netsoltech.com/",
        ats_type="firecrawl",
        keywords=["software engineer", "full stack", "java", "react", "cloud"],
    ),
    CompanyEntry(
        id="10pearls",
        name="10Pearls",
        cities=["Islamabad", "Lahore", "Karachi", "Remote"],
        careers_url="https://10pearls.com/pakistan-job-openings/",
        ats_type="firecrawl",
        keywords=["ai", "machine learning", "full stack", "python", "mobile"],
    ),
    CompanyEntry(
        id="arbisoft",
        name="Arbisoft",
        cities=["Lahore", "Remote"],
        careers_url="https://arbisoft.com/careers/",
        ats_type="firecrawl",
        keywords=["python", "django", "react", "machine learning", "edx", "software engineer"],
    ),
    CompanyEntry(
        id="contour",
        name="Contour Software",
        cities=["Lahore", "Islamabad", "Karachi", "Remote"],
        careers_url="https://contour-software.com/current-openings/",
        ats_type="firecrawl",
        keywords=["software developer", "full stack", "dotnet", "python", "qa"],
    ),
    CompanyEntry(
        id="confiz",
        name="Confiz",
        cities=["Lahore", "Remote"],
        careers_url="https://confiz.simplicant.com/",
        ats_type="firecrawl",
        keywords=["software engineer", "cloud", "react", "python", "ai"],
    ),
    CompanyEntry(
        id="venturedive",
        name="VentureDive",
        cities=["Lahore", "Islamabad", "Karachi", "Remote"],
        careers_url="https://www.venturedive.com/careers/",
        ats_type="firecrawl",
        keywords=["software engineer", "full stack", "react", "python", "mobile"],
    ),
    CompanyEntry(
        id="tkxel",
        name="Tkxel",
        cities=["Lahore", "Remote"],
        careers_url="https://tkxel.com/careers/",
        ats_type="firecrawl",
        keywords=["software engineer", "ai", "full stack", "react", "python"],
    ),
    CompanyEntry(
        id="i2c",
        name="i2c Inc",
        cities=["Lahore", "Remote"],
        careers_url="https://www.i2cinc.com/careers/",
        ats_type="firecrawl",
        keywords=["software engineer", "fintech", "java", "c++", "database"],
    ),
    CompanyEntry(
        id="folio3",
        name="Folio3",
        cities=["Lahore", "Islamabad", "Karachi", "Remote"],
        careers_url="https://folio3.com/careers/",
        ats_type="firecrawl",
        keywords=["ai", "computer vision", "full stack", "python", "mobile"],
    ),
    CompanyEntry(
        id="tintash",
        name="Tintash",
        cities=["Lahore", "Remote"],
        careers_url="https://www.tintash.com/careers",
        ats_type="firecrawl",
        keywords=["software engineer", "python", "react", "node", "ai"],
    ),
    CompanyEntry(
        id="emumba",
        name="Emumba",
        cities=["Islamabad", "Remote"],
        careers_url="https://emumba.com/careers/",
        ats_type="firecrawl",
        keywords=["ai", "llm", "frontend", "full stack", "python", "cloud"],
    ),
    CompanyEntry(
        id="invozone",
        name="InvoZone",
        cities=["Lahore", "Remote"],
        careers_url="https://invozone.com/careers/",
        ats_type="firecrawl",
        keywords=["software developer", "full stack", "react", "python", "nodejs"],
    ),
    CompanyEntry(
        id="motive",
        name="Motive",
        cities=["Lahore", "Islamabad", "Remote"],
        careers_url="https://gomotive.com/company/careers/",
        ats_type="firecrawl",
        keywords=["software engineer", "ai", "computer vision", "full stack", "golang", "python"],
    ),
    CompanyEntry(
        id="educative",
        name="Educative",
        cities=["Lahore", "Remote"],
        careers_url="https://www.educative.io/careers",
        ats_type="firecrawl",
        keywords=["software engineer", "developer advocate", "full stack", "python"],
    ),
    CompanyEntry(
        id="programmersforce",
        name="Programmers Force",
        cities=["Lahore", "Remote"],
        careers_url="https://pf.com.pk/careers",
        ats_type="firecrawl",
        keywords=["ai", "computer vision", "deep learning", "python", "full stack"],
    ),
    CompanyEntry(
        id="curemd",
        name="CureMD",
        cities=["Lahore", "Remote"],
        careers_url="https://www.curemd.com/careers/",
        ats_type="firecrawl",
        keywords=["software engineer", "dotnet", "full stack", "ai", "healthtech"],
    ),
    CompanyEntry(
        id="techlogix",
        name="Techlogix",
        cities=["Lahore", "Islamabad", "Faisalabad", "Karachi", "Remote"],
        careers_url="https://techlogix.com/careers/",
        ats_type="firecrawl",
        keywords=["software engineer", "full stack", "cloud", "java", "fintech"],
    ),
    CompanyEntry(
        id="rolustech",
        name="Rolustech",
        cities=["Lahore", "Remote"],
        careers_url="https://www.rolustech.com/careers/",
        ats_type="firecrawl",
        keywords=["software engineer", "python", "react", "crm", "ai"],
    ),
    CompanyEntry(
        id="avanza",
        name="Avanza Solutions",
        cities=["Lahore", "Karachi", "Remote"],
        careers_url="https://avanzasolutions.com/careers/",
        ats_type="firecrawl",
        keywords=["software engineer", "blockchain", "fintech", "full stack"],
    ),
    CompanyEntry(
        id="ssi",
        name="Strategic Systems International (SSI)",
        cities=["Lahore", "Remote"],
        careers_url="https://ssidecisions.com/careers/",
        ats_type="firecrawl",
        keywords=["software engineer", "ai", "full stack", "python", "cloud"],
    ),
    CompanyEntry(
        id="geniteam",
        name="GenITeam",
        cities=["Lahore", "Remote"],
        careers_url="https://geniteam.com/careers/",
        ats_type="firecrawl",
        keywords=["game developer", "unity", "ai", "full stack", "mobile"],
    ),
    CompanyEntry(
        id="codingpixel",
        name="Coding Pixel",
        cities=["Lahore", "Remote"],
        careers_url="https://codingpixel.com/careers/",
        ats_type="firecrawl",
        keywords=["full stack", "react", "node", "mobile developer"],
    ),
    CompanyEntry(
        id="dpl",
        name="DPL",
        cities=["Islamabad", "Remote"],
        careers_url="https://dpl.com.pk/careers/",
        ats_type="firecrawl",
        keywords=["software engineer", "rebel", "full stack", "python", "ai"],
    ),
    CompanyEntry(
        id="afiniti",
        name="Afiniti",
        cities=["Lahore", "Islamabad", "Karachi", "Remote"],
        careers_url="https://www.afiniti.com/careers",
        ats_type="firecrawl",
        keywords=["ai", "data scientist", "software engineer", "c++", "python"],
    ),
    CompanyEntry(
        id="ovex",
        name="Ovex Technologies",
        cities=["Islamabad", "Lahore", "Remote"],
        careers_url="https://ovextech.com/careers/",
        ats_type="firecrawl",
        keywords=["software engineer", "cloud", "it engineer"],
    ),
    CompanyEntry(
        id="cheetay",
        name="Cheetay",
        cities=["Lahore", "Remote"],
        careers_url="https://cheetay.pk/careers/",
        ats_type="firecrawl",
        keywords=["software engineer", "full stack", "python", "golang"],
    ),
    CompanyEntry(
        id="vopium",
        name="Vopium",
        cities=["Lahore", "Remote"],
        careers_url="https://vopium.com/careers/",
        ats_type="firecrawl",
        keywords=["telecom software", "full stack", "mobile developer"],
    ),
    CompanyEntry(
        id="trg",
        name="TRG Pakistan",
        cities=["Lahore", "Karachi", "Remote"],
        careers_url="https://trgpakistan.com/careers/",
        ats_type="firecrawl",
        keywords=["software engineer", "it", "ai", "cloud"],
    ),
    CompanyEntry(
        id="careem",
        name="Careem",
        cities=["Lahore", "Islamabad", "Karachi", "Remote"],
        careers_url="https://www.careem.com/en-ae/careers/",
        ats_type="firecrawl",
        keywords=["software engineer", "backend", "ios", "android", "full stack"],
    ),
    CompanyEntry(
        id="bramerz",
        name="Bramerz",
        cities=["Lahore", "Remote"],
        careers_url="https://bramerz.pk/careers/",
        ats_type="firecrawl",
        keywords=["digital", "full stack", "react", "php", "python"],
    ),
    CompanyEntry(
        id="northbay",
        name="NorthBay Solutions",
        cities=["Lahore", "Islamabad", "Remote"],
        careers_url="https://northbaysolutions.com/careers/",
        ats_type="firecrawl",
        keywords=["aws", "cloud", "data engineer", "ai", "python"],
    ),
    CompanyEntry(
        id="nextbridge",
        name="Nextbridge",
        cities=["Lahore", "Remote"],
        careers_url="https://nextbridge.com/careers/",
        ats_type="firecrawl",
        keywords=["software engineer", "full stack", "python", "react", "mobile"],
    ),
    CompanyEntry(
        id="arhamsoft",
        name="ArhamSoft",
        cities=["Lahore", "Remote"],
        careers_url="https://www.arhamsoft.com/careers",
        ats_type="firecrawl",
        keywords=["software engineer", "blockchain", "full stack", "react"],
    ),
    CompanyEntry(
        id="purelogics",
        name="PureLogics",
        cities=["Lahore", "Remote"],
        careers_url="https://purelogics.com/careers/",
        ats_type="firecrawl",
        keywords=["full stack", "python", "php", "react", "node"],
    ),
    CompanyEntry(
        id="cinnova",
        name="Cinnova",
        cities=["Lahore", "Remote"],
        careers_url="https://cinnova.com/careers/",
        ats_type="firecrawl",
        keywords=["software engineer", "cloud", "full stack", "dotnet"],
    ),
    CompanyEntry(
        id="sevenol",
        name="Sevenol",
        cities=["Islamabad", "Remote"],
        careers_url="https://sevenol.com/careers/",
        ats_type="firecrawl",
        keywords=["software engineer", "frontend", "full stack"],
    ),
    CompanyEntry(
        id="sadapay",
        name="SadaPay",
        cities=["Islamabad", "Lahore", "Remote"],
        careers_url="https://sadapay.pk/careers",
        ats_type="firecrawl",
        keywords=["backend engineer", "mobile", "flutter", "golang", "fintech"],
    ),
    CompanyEntry(
        id="nayapay",
        name="NayaPay",
        cities=["Karachi", "Lahore", "Remote"],
        careers_url="https://nayapay.com/careers",
        ats_type="firecrawl",
        keywords=["software engineer", "fintech", "backend", "security"],
    ),
    CompanyEntry(
        id="bazaar",
        name="Bazaar Technologies",
        cities=["Lahore", "Karachi", "Remote"],
        careers_url="https://bazaartech.bamboohr.com/careers",
        ats_type="firecrawl",
        keywords=["software engineer", "python", "react", "product"],
    ),
    CompanyEntry(
        id="tajir",
        name="Tajir",
        cities=["Lahore", "Remote"],
        careers_url="https://tajir.app/careers",
        ats_type="firecrawl",
        keywords=["software engineer", "full stack", "python", "mobile"],
    ),
    CompanyEntry(
        id="bykea",
        name="Bykea",
        cities=["Lahore", "Karachi", "Rawalpindi", "Remote"],
        careers_url="https://bykea.com/careers/",
        ats_type="firecrawl",
        keywords=["backend engineer", "mobile", "nodejs", "python"],
    ),
    CompanyEntry(
        id="daraz",
        name="Daraz (Alibaba Group)",
        cities=["Lahore", "Karachi", "Remote"],
        careers_url="https://careers.daraz.com/",
        ats_type="firecrawl",
        keywords=["software engineer", "full stack", "java", "react", "ecommerce"],
    ),
    CompanyEntry(
        id="zameen",
        name="Zameen.com / EMPG",
        cities=["Lahore", "Islamabad", "Faisalabad", "Karachi", "Remote"],
        careers_url="https://careers.zameen.com/",
        ats_type="firecrawl",
        keywords=["software engineer", "full stack", "php", "react", "python"],
    ),
    CompanyEntry(
        id="pakwheels",
        name="PakWheels",
        cities=["Lahore", "Karachi", "Islamabad", "Remote"],
        careers_url="https://pakwheels.com/careers",
        ats_type="firecrawl",
        keywords=["software engineer", "ror", "ruby", "react", "mobile"],
    ),
    CompanyEntry(
        id="dubizzle",
        name="Dubizzle Labs",
        cities=["Lahore", "Remote"],
        careers_url="https://dubizzlelabs.com/careers",
        ats_type="firecrawl",
        keywords=["software engineer", "full stack", "python", "cloud"],
    ),
    CompanyEntry(
        id="finja",
        name="Finja",
        cities=["Lahore", "Remote"],
        careers_url="https://finja.pk/careers",
        ats_type="firecrawl",
        keywords=["fintech", "software engineer", "mobile", "backend"],
    ),
    CompanyEntry(
        id="creditbook",
        name="CreditBook",
        cities=["Karachi", "Lahore", "Remote"],
        careers_url="https://creditbook.pk/careers",
        ats_type="firecrawl",
        keywords=["software engineer", "flutter", "react", "golang"],
    ),
    CompanyEntry(
        id="telenor",
        name="Telenor Pakistan",
        cities=["Islamabad", "Lahore", "Remote"],
        careers_url="https://telenor.com.pk/careers",
        ats_type="firecrawl",
        keywords=["software engineer", "data engineer", "cloud", "ai"],
    ),
    CompanyEntry(
        id="jazz",
        name="Jazz / Veon",
        cities=["Islamabad", "Lahore", "Remote"],
        careers_url="https://jazz.com.pk/careers",
        ats_type="firecrawl",
        keywords=["software engineer", "cloud", "ai", "digital", "python"],
    ),
    CompanyEntry(
        id="ptcl",
        name="PTCL / Ufone",
        cities=["Islamabad", "Lahore", "Remote"],
        careers_url="https://ptcl.com.pk/careers",
        ats_type="firecrawl",
        keywords=["software engineer", "network", "devops", "cloud"],
    ),
    CompanyEntry(
        id="sofizar",
        name="Sofizar",
        cities=["Lahore", "Remote"],
        careers_url="https://sofizar.com/careers",
        ats_type="firecrawl",
        keywords=["software engineer", "ecommerce", "python", "php"],
    ),
    CompanyEntry(
        id="brainx",
        name="BrainX Technologies",
        cities=["Lahore", "Remote"],
        careers_url="https://brainxtech.com/careers/",
        ats_type="firecrawl",
        keywords=["software engineer", "full stack", "react", "ai"],
    ),
    CompanyEntry(
        id="qsoft",
        name="Q-Soft Technologies",
        cities=["Lahore", "Remote"],
        careers_url="https://qsofttech.com/careers/",
        ats_type="firecrawl",
        keywords=["software developer", "dotnet", "full stack"],
    ),
    CompanyEntry(
        id="octos",
        name="Octos Global",
        cities=["Lahore", "Islamabad", "Remote"],
        careers_url="https://octosglobal.com/careers/",
        ats_type="firecrawl",
        keywords=["software engineer", "cloud", "mobile"],
    ),
    CompanyEntry(
        id="eureka",
        name="Eureka Technology Partners",
        cities=["Lahore", "Remote"],
        careers_url="https://eurekatech.com/careers",
        ats_type="firecrawl",
        keywords=["software developer", "it services"],
    ),
    CompanyEntry(
        id="pixelsoftwares",
        name="Pixel Softwares",
        cities=["Faisalabad", "Lahore", "Remote"],
        careers_url="https://pixelsoftwares.com/careers/",
        ats_type="firecrawl",
        keywords=["software engineer", "faisalabad", "full stack", "react"],
    ),
    CompanyEntry(
        id="xgrid",
        name="Xgrid",
        cities=["Islamabad", "Remote"],
        careers_url="https://xgrid.co/careers/",
        ats_type="firecrawl",
        keywords=["cloud", "devops", "software engineer", "kubernetes"],
    ),
    CompanyEntry(
        id="experlabs",
        name="Exper Labs",
        cities=["Lahore", "Remote"],
        careers_url="https://experlabs.com/careers",
        ats_type="firecrawl",
        keywords=["software engineer", "ror", "full stack", "react"],
    ),
    CompanyEntry(
        id="qbxnet",
        name="QBXNet",
        cities=["Lahore", "Remote"],
        careers_url="https://qbxnet.com/careers",
        ats_type="firecrawl",
        keywords=["software engineer", "junior developer", "full stack"],
    ),
    CompanyEntry(
        id="repairdesk",
        name="RepairDesk",
        cities=["Lahore", "Remote"],
        careers_url="https://www.repairdesk.co/careers",
        ats_type="firecrawl",
        keywords=["full stack engineer", "node", "vue", "php"],
    ),
    CompanyEntry(
        id="mitsolutions",
        name="MIT Solutions",
        cities=["Lahore", "Remote"],
        careers_url="https://mitsolutions.com.pk/careers",
        ats_type="firecrawl",
        keywords=["full stack developer", "software engineer"],
    ),
    CompanyEntry(
        id="aqovia",
        name="Aqovia",
        cities=["Lahore", "Remote"],
        careers_url="https://aqovia.com/careers",
        ats_type="firecrawl",
        keywords=["software engineer", "c#", "dotnet", "cloud"],
    ),
    CompanyEntry(
        id="iyrix",
        name="Iyrix Technologies",
        cities=["Lahore", "Remote"],
        careers_url="https://iyrix.com/careers",
        ats_type="firecrawl",
        keywords=["react engineer", "frontend", "full stack"],
    ),
    CompanyEntry(
        id="jahaann",
        name="Jahaann",
        cities=["Lahore", "Islamabad", "Remote"],
        careers_url="https://jahaann.com/careers",
        ats_type="firecrawl",
        keywords=["devops", "cloud", "software engineer"],
    ),
    CompanyEntry(
        id="overjet",
        name="Overjet",
        cities=["Lahore", "Remote"],
        careers_url="https://www.overjet.ai/careers",
        ats_type="firecrawl",
        keywords=["ai", "computer vision", "software engineer"],
    ),
    CompanyEntry(
        id="codeninja",
        name="CodeNinja",
        cities=["Lahore", "Remote"],
        careers_url="https://codeninja.io/careers",
        ats_type="firecrawl",
        keywords=["software engineer", "cloud", "full stack", "ai"],
    ),
    CompanyEntry(
        id="apex47",
        name="Apex47",
        cities=["Faisalabad", "Remote"],
        careers_url="https://apex47.com/careers",
        ats_type="firecrawl",
        keywords=["faisalabad", "software engineer", "full stack", "mobile"],
    ),
    CompanyEntry(
        id="microtech",
        name="MicroTech Industries",
        cities=["Islamabad", "Lahore", "Remote"],
        careers_url="https://microtech.com.pk/careers",
        ats_type="firecrawl",
        keywords=["embedded", "iot", "software engineer", "firmware"],
    ),
    CompanyEntry(
        id="abacus",
        name="Abacus Consulting",
        cities=["Lahore", "Islamabad", "Karachi", "Remote"],
        careers_url="https://abacus-global.com/careers",
        ats_type="firecrawl",
        keywords=["software qa", "software engineer", "cloud", "erp", "consulting"],
    ),
    CompanyEntry(
        id="devlinesolutions",
        name="Devline Solutions",
        cities=["Lahore", "Remote"],
        careers_url="https://devlinesolutions.com/career",
        ats_type="firecrawl",
        keywords=["full stack", "developer", "react", "python", "software"],
    ),
]


def get_companies_for_city(city: str) -> list[CompanyEntry]:
    """Filter registry for companies having offices in the given city (e.g. 'Lahore', 'Islamabad', 'Faisalabad')."""
    city_lower = city.lower().strip()
    return [
        c for c in TOP_PAKISTAN_TECH_COMPANIES
        if any(city_lower in x.lower() for x in c.cities)
    ]


def find_company_by_name(name: str) -> CompanyEntry | None:
    """Find registered company entry by company name or aliases (case-insensitive fuzzy match)."""
    if not name:
        return None
    name_clean = name.lower().strip()
    # Remove common suffixes like pvt ltd, inc, technologies, llc, pvt. ltd.
    name_normalized = (
        name_clean
        .replace("pvt ltd", "")
        .replace("pvt. ltd.", "")
        .replace("private limited", "")
        .replace("technologies", "")
        .replace("solutions", "")
        .replace("technology", "")
        .replace("labs", "")
        .replace("inc.", "")
        .replace("inc", "")
        .replace("ltd.", "")
        .replace("ltd", "")
        .replace("corp", "")
        .replace("llc", "")
        .strip()
    )

    # 1. Exact match on company ID or name
    for c in TOP_PAKISTAN_TECH_COMPANIES:
        if c.id == name_clean or c.name.lower() == name_clean:
            return c

    # 2. Substring or normalized match
    for c in TOP_PAKISTAN_TECH_COMPANIES:
        c_clean = c.name.lower()
        if name_normalized and (name_normalized in c_clean or c_clean in name_clean):
            return c
        if c.id in name_clean:
            return c

    return None

