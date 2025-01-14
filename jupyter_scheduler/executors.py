import os
import traceback
from abc import ABC, abstractmethod
from typing import Dict

import nbconvert
import nbformat
from nbconvert.preprocessors import ExecutePreprocessor

from jupyter_scheduler.config import ExecutionConfig
from jupyter_scheduler.models import DescribeJob, JobFeature, Status
from jupyter_scheduler.orm import Job, create_session
from jupyter_scheduler.parameterize import add_parameters
from jupyter_scheduler.utils import get_utc_timestamp, resolve_path


class ExecutionManager(ABC):
    """Base execution manager.
    Clients are expected to override this class
    to provide concrete implementations of the
    execution manager. At the minimum, subclasses
    should provide implementation of the
    execute, and supported_features methods.
    """

    _model = None
    _db_session = None

    def __init__(self, job_id: str, config: ExecutionConfig = {}):
        self.job_id = job_id
        self.root_dir = config.root_dir
        self.config = config

    @property
    def model(self):
        if self._model is None:
            with self.db_session() as session:
                job = session.query(Job).filter(Job.job_id == self.job_id).first()
                self._model = DescribeJob.from_orm(job)
        return self._model

    @property
    def db_session(self):
        if self._db_session is None:
            self._db_session = create_session(self.config.db_url)

        return self._db_session

    def process(self):
        """The template method called by the
        Scheduler, backend implementations
        should not override this method.
        """
        self.before_start()
        try:
            self.execute()
        except Exception as e:
            self.on_failure(e)
        else:
            self.on_complete()

    @abstractmethod
    def execute(self):
        """Performs notebook execution,
        custom backends are expected to
        add notebook execution logic within
        this method
        """
        pass

    @classmethod
    @abstractmethod
    def supported_features(cls) -> Dict[JobFeature, bool]:
        """Returns a configuration of supported features
        by the execution engine. Implementors are expected
        to override this to return a dictionary of supported
        job creation features.
        """
        pass

    def before_start(self):
        """Called before start of execute"""
        job = self.model
        with self.db_session() as session:
            session.query(Job).filter(Job.job_id == job.job_id).update(
                {"start_time": get_utc_timestamp(), "status": Status.IN_PROGRESS}
            )
            session.commit()

    def on_failure(self, e: Exception):
        """Called after failure of execute"""
        job = self.model
        with self.db_session() as session:
            session.query(Job).filter(Job.job_id == job.job_id).update(
                {"status": Status.FAILED, "status_message": str(e)}
            )
            session.commit()

        traceback.print_exc()

    def on_complete(self):
        """Called after job is completed"""
        job = self.model
        with self.db_session() as session:
            session.query(Job).filter(Job.job_id == job.job_id).update(
                {"status": Status.COMPLETED, "end_time": get_utc_timestamp()}
            )
            session.commit()


class DefaultExecutionManager(ExecutionManager):
    """Default execution manager that executes notebooks"""

    def execute(self):
        job = self.model

        output_dir = os.path.dirname(resolve_path(job.output_uri, self.root_dir))
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)

        with open(resolve_path(job.input_uri, self.root_dir)) as f:
            nb = nbformat.read(f, as_version=4)

        if job.parameters:
            nb = add_parameters(nb, job.parameters)

        ep = ExecutePreprocessor(
            timeout=job.timeout_seconds,
            kernel_name=nb.metadata.kernelspec["name"],
            store_widget_state=True,
        )

        ep.preprocess(
            nb, {"metadata": {"path": os.path.dirname(resolve_path(job.output_uri, self.root_dir))}}
        )

        if job.output_formats:
            filepath = resolve_path(job.output_uri, self.root_dir)
            base_filepath = os.path.splitext(filepath)[-2]
            for output_format in job.output_formats:
                cls = nbconvert.get_exporter(output_format)
                output, resources = cls().from_notebook_node(nb)
                with open(f"{base_filepath}.{output_format}", "w", encoding="utf-8") as f:
                    f.write(output)
        else:
            with open(resolve_path(job.output_uri, self.root_dir), "w", encoding="utf-8") as f:
                nbformat.write(nb, f)

    def supported_features(cls) -> Dict[JobFeature, bool]:
        return {
            JobFeature.job_name: True,
            JobFeature.output_formats: True,
            JobFeature.job_definition: False,
            JobFeature.idempotency_token: False,
            JobFeature.tags: False,
            JobFeature.email_notifications: False,
            JobFeature.timeout_seconds: False,
            JobFeature.retry_on_timeout: False,
            JobFeature.max_retries: False,
            JobFeature.min_retry_interval_millis: False,
            JobFeature.output_filename_template: False,
            JobFeature.stop_job: True,
            JobFeature.delete_job: True,
        }
