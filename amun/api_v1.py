#!/usr/bin/env python3
# Amun
# Copyright(C) 2018, 2019, 2020 Fridolin Pokorny
#
# This program is free software: you can redistribute it and / or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.

"""Implementation of API v1."""

import os
import logging
import json
import random
import re
from urllib.parse import urlparse

from deprecated.sphinx import deprecated

from thoth.common import OpenShift
from thoth.common import WorkflowManager
from thoth.common import datetime2datetime_str
from thoth.common.exceptions import NotFoundException

from .configuration import Configuration
from .dockerfile import create_dockerfile
from .exceptions import ScriptObtainingError

_LOGGER = logging.getLogger(__name__)

_OPENSHIFT = OpenShift()

# These are default requests for inspection builds and runs if not stated
# otherwise. We explicitly assign defaults to requests coming to API so that
# the specification always carries these values in inspection documents.
_DEFAULT_REQUESTS = {"cpu": "500m", "memory": "256Mi"}


def _construct_parameters_dict(specification: dict) -> tuple:
    """Construct parameters that should be passed to build or inspection job."""
    # Name of parameters are shared in build/job templates so parameters are constructed regardless build or job.
    parameters = {}
    use_hw_template = False
    if "hardware" in specification.get("requests", {}):
        hardware_specification = specification.get("requests", {}).get("hardware", {})
        use_hw_template = True

        if "cpu_family" in hardware_specification:
            parameters["CPU_FAMILY"] = hardware_specification["cpu_family"]

        if "cpu_model" in hardware_specification:
            parameters["CPU_MODEL"] = hardware_specification["cpu_model"]

        if "physical_cpus" in hardware_specification:
            parameters["PHYSICAL_CPUS"] = hardware_specification["physical_cpus"]

        if "processor" in hardware_specification:
            parameters["PROCESSOR"] = hardware_specification["processor"]

    return parameters, use_hw_template


def _do_create_dockerfile(specification: dict) -> tuple:
    """Wrap dockerfile generation and report back an error if any."""
    try:
        return create_dockerfile(specification)
    except ScriptObtainingError as exc:
        return None, str(exc)


def post_generate_dockerfile(specification: dict):
    """Generate Dockerfile out of software stack specification."""
    parameters = {"specification": specification}

    dockerfile, error = _do_create_dockerfile(specification)
    if dockerfile is None:
        return {"parameters": parameters, "error": error}, 400

    return {"parameters": parameters, "dockerfile": dockerfile}, 200


def _adjust_default_requests(dict_: dict) -> None:
    """Explicitly assign default requests so that they are carried within the requested inspection run."""
    if "requests" not in dict_:
        dict_["requests"] = {}

    dict_["requests"]["cpu"] = dict_["requests"].get("cpu") or _DEFAULT_REQUESTS["cpu"]
    dict_["requests"]["memory"] = dict_["requests"].get("memory") or _DEFAULT_REQUESTS["memory"]


def _parse_specification(specification: dict) -> dict:
    """Parse inspection specification.

    Cast types to comply with Argo and escapes quotes.
    """
    parsed_specification = specification.copy()

    def _escape_single_quotes(obj):
        if isinstance(obj, dict):
            for k in obj:
                obj[k] = _escape_single_quotes(obj[k])
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                obj[i] = _escape_single_quotes(v)
        elif isinstance(obj, str):
            return re.sub(r"'(?!')", "''", obj)

        return obj

    parsed_specification = _escape_single_quotes(parsed_specification)

    int_to_str = ["allowed_failures", "batch_size", "parallelism"]
    for key in int_to_str:
        if key not in specification:
            continue

        parsed_specification[key] = str(specification[key])

    if "build" not in parsed_specification:
        parsed_specification["build"] = {}

    if "run" not in parsed_specification:
        parsed_specification["run"] = {}

    return parsed_specification


def _unparse_specification(parsed_specification: dict) -> dict:
    """Unparse inspection specification.

    Casts types to comply with the inspection scheme and unescapes quotes.
    """
    specification = parsed_specification.copy()

    def _unescape_single_quotes(obj):
        if isinstance(obj, dict):
            for k in obj:
                obj[k] = _unescape_single_quotes(obj[k])
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                obj[i] = _unescape_single_quotes(v)
        elif isinstance(obj, str):
            return re.sub(r"''", "'", obj)

        return obj

    specification = _unescape_single_quotes(specification)

    str_to_int = ["allowed_failures", "batch_size", "parallelism"]
    for key in str_to_int:
        if key not in specification:
            continue

        specification[key] = int(parsed_specification[key])

    return specification


def post_inspection(specification: dict) -> tuple:
    """Create new inspection for the given software stack."""
    # Generate first Dockerfile so we do not end up with an empty imagestream if Dockerfile creation fails.
    dockerfile, run_job_or_error = _do_create_dockerfile(specification)
    if dockerfile is None:
        return (
            {
                "parameters:": specification,
                # If not dockerfile is produced, run_job holds the error message.
                "error": run_job_or_error,
            },
            400,
        )

    run_job = run_job_or_error

    specification = _parse_specification(specification)

    _adjust_default_requests(specification["run"])
    _adjust_default_requests(specification["build"])

    parameters, use_hw_template = _construct_parameters_dict(specification.get("build", {}))

    # Mark this for later use - in get_inspection_specification().
    specification["@created"] = datetime2datetime_str()

    target = "inspection-run-result" if run_job else "inspection-build"

    dockerfile = dockerfile.replace("'", "''")

    workflow_id = _OPENSHIFT.schedule_inspection(
        dockerfile=dockerfile,
        specification=specification,
        target=target,
        parameters=parameters
    )

    # TODO: Check whether the workflow spec has been resolved successfully
    # The resolution happens on the server side, therefore even if the WF
    # is submitted successfully, it mail fail due to an invalid spec later on

    return (
        {
            "inspection_id": inspection_id,
            "parameters": specification,
            "workflow_id": workflow_id,
            "workflow_target": target,
        },
        202,
    )


@deprecated(
    version="0.6.0",
    reason=(
        "The function will be removed soon."
        "The functionality is limited to a single inspection, i.e. `batch_size = 1`."
    ),
)
def get_inspection_job_log(inspection_id: str) -> tuple:
    """Get logs of the given inspection."""
    parameters = {"inspection_id": inspection_id}
    try:
        log = _OPENSHIFT.get_job_log(inspection_id, Configuration.AMUN_INSPECTION_NAMESPACE)
    except NotFoundException as exc:
        try:
            return (
                {"error": "No logs available yet for the given inspection id", "parameters": parameters},
                202,
            )
        except NotFoundException:
            pass

        return (
            {"error": "Job log for the given inspection id was not found", "parameters": parameters},
            404,
        )

    if not log:
        return (
            {
                "error": "Inspection run did not produce any log or it was deleted by OpenShift",
                "parameters": parameters,
            },
            404,
        )

    try:
        log = json.loads(log)
    except Exception as exc:
        _LOGGER.exception("Failed to load inspection job log for %r", inspection_id)
        return (
            {"error": "Job failed, please contact administrator for more details", "parameters": parameters},
            500,
        )

    return {"log": log, "parameters": parameters}, 200


def get_inspection_job_logs(inspection_id: str) -> tuple:
    """Get logs of the given inspection."""
    parameters = {"inspection_id": inspection_id}

    response, _ = get_inspection_status(inspection_id)
    inspection_status: Dict[str, Any] = response["status"]

    _LOGGER.debug("Inspection Workflow '%s' status: %r", inspection_id, inspection_status)
    if not inspection_status["build"].get("state") == "terminated":
        return (
            {
                "error": "No logs available yet for the given inspection id",
                "status": inspection_status,
                "parameters": parameters,
            },
            202,
        )

    pod_logs: List[str] = []
    try:
        pod_ids: List[str] = _OPENSHIFT._get_pod_ids_from_job(inspection_id, Configuration.AMUN_INSPECTION_NAMESPACE)

        for pod_id in pod_ids:
            log: str = _OPENSHIFT.get_pod_log(pod_id, namespace=Configuration.AMUN_INSPECTION_NAMESPACE)
            pod_logs.append(log)
    except NotFoundException as exc:
        return (
            {
                "error": "No pods for the given inspection id was not found",
                "status": inspection_status,
                "parameters": parameters,
            },
            404,
        )

    inspection_logs: List[Dict[str, Any]] = []
    for pod, pod_log in zip(pod_ids, pod_logs):
        log: Dict[str, Any]
        try:
            log = json.loads(pod_log)
        except json.JSONDecodeError:
            _LOGGER.exception("Failed to parse log from pod %s: %r", pod, pod_log)
            continue

        inspection_logs.append(log)

    if not any(inspection_logs):
        _LOGGER.error("Inspection run did not produce any logs or it was deleted by OpenShift")
        return (
            {
                "error": "Inspection run did not produce any logs or it was deleted by OpenShift",
                "status": inspection_status,
                "parameters": parameters,
            },
            404,
        )

    return {"logs": inspection_logs, "parameters": parameters}, 200


def get_inspection_build_log(inspection_id: str) -> tuple:
    """Get build log of an inspection."""
    parameters = {"inspection_id": inspection_id}

    try:
        status = _OPENSHIFT.get_pod_log(inspection_id + "-1-build", Configuration.AMUN_INSPECTION_NAMESPACE)
    except NotFoundException:
        return (
            {"error": "Build log with for the given inspection id was not found", "parameters": parameters},
            404,
        )

    return {"log": status, "parameters": parameters}, 200


def get_inspection_status(inspection_id: str) -> tuple:
    """Get status of an inspection."""
    parameters = {"inspection_id": inspection_id}

    workflow_status = None
    try:
        wf: Dict[str, Any] = _OPENSHIFT.get_workflow(
            label_selector=f"inspection_id={inspection_id}", namespace=_OPENSHIFT.amun_inspection_namespace,
        )
        workflow_status = wf["status"]
    except NotFoundException as exc:
        return {
            "error": "A Workflow for the given inspection id was not found",
            "parameters": parameters,
        }, 404

    build_status = None
    try:
        # As we treat inspection_id same all over the places (dc, dc, job), we can
        # safely call gathering info about pod. There will be always only one build
        # (hopefully) - created per a user request.
        # OpenShift does not expose any endpoint for a build status anyway.
        build_status = _OPENSHIFT.get_pod_status_report(
            inspection_id + "-1-build", Configuration.AMUN_INSPECTION_NAMESPACE
        )
    except NotFoundException:
        return (
            {"error": "The given inspection id was not found", "parameters": parameters},
            404,
        )

    job_status = None
    try:
        job_status = _OPENSHIFT.get_job_status_report(inspection_id, Configuration.AMUN_INSPECTION_NAMESPACE)
    except NotFoundException:
        # There was no job scheduled - user did not submitted any script to run the job. Report None.
        pass

    return (
        {"status": {"build": build_status, "job": job_status, "workflow": workflow_status}, "parameters": parameters},
        200,
    )


def get_inspection_specification(inspection_id: str):
    """Get specification for the given build."""
    parameters = {"inspection_id": inspection_id}

    try:
        wf: Dict[str, Any] = _OPENSHIFT.get_workflow(
            label_selector=f"inspection_id={inspection_id}", namespace=_OPENSHIFT.amun_inspection_namespace,
        )
    except NotFoundException as exc:
        return {
            "error": "A Workflow for the given inspection id as not found",
            "parameters": parameters,
        }

    parameters: List[Dict[str, Any]] = wf["spec"]["arguments"]["parameters"]

    (specification_parameter,) = filter(lambda p: p["name"] == "specification", parameters)
    specification = specification_parameter["value"]
    specification = json.loads(specification)
    specification = _unparse_specification(specification)

    # We inserted created information on our own, pop it not to taint the original specification request.
    created = specification.pop("@created")
    return {
        "parameters": parameters,
        "specification": specification,
        "created": created,
    }
