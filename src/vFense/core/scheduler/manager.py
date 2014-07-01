#!/usr/bin/env python
import json
import logging
import logging.config
from vFense import VFENSE_LOGGING_CONFIG
import apscheduler

from datetime import datetime
from copy import deepcopy
from pytc import utc
from apscheduler.jobstores.rethinkdb_store import RethinkDBJobStore
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.schedulers.tornado import TornadoScheduler

logging.config.fileConfig(VFENSE_LOGGING_CONFIG)
logger = logging.getLogger('rvapi')


def start_scheduler(scheduler_type='tornado', collection='jobs'):
    if scheduler_type == 'tornado':
        Scheduler = TornadoScheduler
    else:
        Scheduler = BackgroundScheduler


    jobstore = {'default': RethinkDBJobStore(table=collection)}
    job_defaults = {'coalesce': True}
    sched = (
        Scheduler(
            jobstores=jobstore, job_defaults=job_defaults, timezone=utc
        )
    )
    try:
        sched.start()
    except Exception as e:
        logger.exception(e)
        sched = None

    return sched


def stop_scheduler(sched):
    stopped = False

    try:
        logger.info('Attempting to shutdown the Scheduler')
        sched.shutdown()
        logger.info('Scheduler has shutdown')
        stopped = True

    except Exception as e:
        logger.error('Failed to shutdown the Scheduler')

    return stopped


def restart_scheduler():
    stop = stop_scheduler()
    sched = None

    if stop:
        start = start_scheduler()

        if start:
            sched = start
            logger.info('Scheduler restarted successfully')

    return sched


class ScheduleManager(object):
    def __init__(self, schedule, view_name):
        self.schedule = schedule
        self.view_name = view_name
        self.jobs = self.get_jobs(view_name)

    def get_jobs(self, view_name=None):
        if view_name:
            jobs = self.schedule.get_jobs(name=view_name)
        else:
            jobs = self.schedule.get_jobs(name=self.view_name)

        return jobs

    def job_exist(self, name):
        exist = False
        for schedule in self.jobs:
            if name in schedule.name:
                exist = True

        return exist


@db_create_close
def job_lister(sched, view_name, username, uri=None, method=None,
        conn=None):

    jobs = sched.get_jobs(name=view_name)
    job_listing = []
    username = None
    cname = None
    agents = []
    tags = []

    for schedule in jobs:
        job = None
        cp_of_sched = deepcopy(schedule.args)

        if len(schedule.args) > 1:
            username = cp_of_sched.pop()
            cname = cp_of_sched.pop()

        if cp_of_sched:
            job = cp_of_sched.pop(0)
        else:
            continue

        if not isinstance(job, dict):
            json_is_valid, json_job = verify_json_is_valid(job)
        else:
            json_job = job
            json_is_valid = isinstance(json_job, dict)

        if isinstance(
            schedule.trigger, apscheduler.triggers.cron.CronTrigger
        ):
            schedule_type = 'cron'

        elif isinstance(
            schedule.trigger, apscheduler.triggers.interval.IntervalTrigger
        ):
            schedule_type = 'interval'

        elif isinstance(
            schedule.trigger, apscheduler.triggers.simple.SimpleTrigger
        ):
            schedule_type = 'once'

        json_job['next_run_time'] = str(schedule.next_run_time)
        json_job['job_name'] = schedule.name
        json_job['runs'] = schedule.runs
        json_job['username'] = username
        json_job['view_name'] = view_name
        json_job['schedule_type'] = schedule_type
        json_job['uri'] = uri
        json_job['method'] = method
        tag_ids = json_job.pop('tag_ids')
        agent_ids = json_job.pop('agent_ids')

        job_listing.append(json_job)

    try:
        data = job_listing
        results = GenericResults(
            username, uri, method,
        ).information_retrieved(data, len(data))

    except Exception as e:
        logger.exception(e)
        results = GenericResults(
            username, uri, method
        ).something_broke(
            json_job['job_name'], 'failed to retrieve data', e
        )

    return results

@db_create_close
def get_agentids_per_job(job_info, view_name, username,  conn=None):

    agent_ids = job_info['agent_ids']
    tag_ids = job_info['tag_ids']
    all_agents = job_info['all_agents']
    all_tags = job_info['all_tags']

    if (all_agents and not job_info['agent_ids']
            and not job_info['tag_ids'] and not all_tags):
        agent_ids = fetch_agent_ids_in_views([view_name])

    elif (job_info['agent_ids'] and not all_agents
            and not job_info['tag_ids'] and not all_tags):
        agent_ids = agent_ids

    elif (all_tags and not all_agents and not
            job_info['agent_ids'] and not job_info['tag_ids']):
        agent_ids = []
        tag_ids = fetch_tag_ids(view_name)

        if tag_ids:
            for tag_id in tag_ids:
                agent_ids = agent_ids + fetch_agent_ids_in_tag(tag_id)

        agent_ids = list(set(agent_ids))

    elif (job_info['tag_ids'] and not all_agents and not
            job_info['agent_ids'] and not all_tags):
        agent_ids = []
        tag_ids = tag_ids

        for tag_id in tag_ids:
            agent_ids = agent_ids + fetch_agent_ids_in_tag(tag_id)

        agent_ids = list(set(agent_ids))

    return agent_ids


def get_tags_per_job(job_info, username, view_name, conn=None):

    tags = []
    keys_to_pluck = [TagKeys.TagId, TagKeys.TagName]

    if job_info['all_tags']:
        tags = fetch_tags_by_view(view_name, keys_to_pluck)

    elif job_info['tag_ids']:
        tags = fetch_tags_by_id(job_info['tag_ids'], keys_to_pluck)

    return tags


def remove_job(sched, jobname, view_name, username, uri=None, method=None):

    jobs = sched.get_jobs(name=view_name)
    count = 0

    for schedule in jobs:
        if jobname in schedule.name:
            try:
                sched.unschedule_job(schedule)
                count = count + 1
                results = (
                    SchedulerResults(username, uri, method)
                    .removed(jobname)
                )

            except Exception as e:
                logger.exception(e)
                count = count + 1
                results = (
                    SchedulerResults(username, uri, method)
                    .remove_failed(jobname)
                )

    if count == 0:
        results = (
            SchedulerResults(username, uri, method)
            .invalid_schedule_name(jobname)
        )

    logger.info(results)

    return results


def get_appid_list(agent_id, severity=None,
        collection=AppCollections.AppsPerAgent):

    severities = ['Critical', 'Recommended', 'Optional']

    if severity:
        severity = severity.capitalize()

    if severity in severities:
        appids = fetch_appids_by_agentid_and_status(
            agent_id, CommonAppKeys.AVAILABLE, severity, collection
        )

    else:
        appids = fetch_appids_by_agentid_and_status(
            agent_id, CommonAppKeys.AVAILABLE, collection=collection
        )

    return appids


def get_app_for_appids(collection, app_id, conn=None):
    fields_to_pluck = [AppsKey.AppId, AppsKey.Name, AppsKey.RvSeverity]
    app = fetch_app_data(
        app_id, collection=collection, fields_to_pluck=fields_to_pluck
    )

    return app

@db_create_close
def get_agent_apps_details(job, agent_id, details=True, conn=None):
    app_ids = []
    app_ids_needed = []
    apps = []
    app_keys_to_pluck = [
        AppsKey.AppId,
        AppsKey.Name,
        AppsKey.RvSeverity
    ]

    if job['pkg_type'] == 'system_apps':
        CurrentAppsPerAgentCollection = AppCollections.AppsPerAgent
        CurrentAppsCollection = AppCollections.UniqueApplications

    elif job['pkg_type'] == 'custom_apps':
        CurrentAppsPerAgentCollection = AppCollections.CustomAppsPerAgent
        CurrentAppsCollection = AppCollections.CustomApps

    elif job['pkg_type'] == 'supported_apps':
        CurrentAppsPerAgentCollection = AppCollections.SupportedAppsPerAgent
        CurrentAppsCollection = AppCollections.SupportedApps

    if not job.get('app_ids', None):
        app_ids = get_appid_list(
            agent_id, job['severity'], CurrentAppsPerAgentCollection
        )

    else:
        app_ids = job['app_ids']

    for app_id in app_ids:
        app_info = list(
            r
            .table(CurrentAppsCollection)
            .filter(
                {
                    AppsKey.AppId:app_id
                }
            )
            .pluck(AppsKey.Hidden)
            .run(conn)
        )

        if app_info[0][AppsKey.Hidden] == 'no':
            app_ids_needed.append(app_id)

    if app_ids_needed and details:
        apps = fetch_app_data_by_appids(
            app_ids_needed, collection=CurrentAppsCollection,
            fields_to_pluck=app_keys_to_pluck
        )

    elif app_ids_needed and not details:
        apps = app_ids_needed

    return apps


def get_schedule_details(sched, job_name, username, view_name,
    uri=None, method=None, conn=None):

    agents = []
    apps = []
    app_details = []
    tags = []

    jobs = sched.get_jobs(name=view_name)
    agent_keys_to_pluck = [
        AgentKeys.ComputerName,
        AgentKeys.DisplayName,
        AgentKeys.AgentId
    ]

    try:
        for schedule in jobs:
            if job_name == schedule.name:
                job = schedule.args[0]

                if job:
                    tags = get_tags_per_job(
                        job_info=job, username=username,
                        view_name=view_name
                    )
                    agent_ids = get_agentids_per_job(
                        job_info=job,
                        view_name=view_name,
                        username=username
                    )

                    if agent_ids:
                        for agent_id in agent_ids:
                            agent_id = agent_id
                            agent = fetch_agent(
                                agent_id, agent_keys_to_pluck
                            )

                            if job['operation'] == 'install':
                                apps = get_agent_apps_details(job, agent_id)
                                agent['apps'] = apps
                                agents.append(agent)

                            else:
                                agents.append(agent)

                    data = {
                        'agents': agents,
                        'tags': tags,
                        'next_run_time': str(schedule.next_run_time),
                        'job_name': schedule.name,
                        'runs': schedule.runs,
                        'username': username,
                        'view_name': view_name,
                        'operation': job['operation'],
                    }

                    if job['operation'] == 'install':
                        data['pkg_type'] = job['pkg_type']
                        data['severity'] = job['severity']

                    elif job['operation'] == 'reboot':
                        data['pkg_type'] = None
                        data['severity'] = None

                    results = GenericResults(
                        username, uri, method,
                    ).information_retrieved(data, len(data))


    except Exception as e:
        logger.exception(e)
        results = GenericResults(
            username, uri, method
        ).something_broke('schedules', 'failed to retrieve data', e)

    return results

def scheduled_install_operation(job_info, view_name, username,
        uri=None, method=None):

    agent_ids = job_info['agent_ids']
    tag_ids = job_info['tag_ids']

    if job_info['severity']:
        severity = job_info['severity'].capitalize()
    else:
        severity = None

    jobname = job_info['job_name']

    store_operation = StorePatchingOperation(
        username, view_name, uri, method
    )

    if not agent_ids:
        agent_ids = []

    if not tag_ids:
        tag_ids = []

    agent_ids = get_agentids_per_job(
        job_info=job_info, username=username, view_name=view_name
    )

    msg = (
        "%s - Scheduled job %s is in the process"
        "of running on the following agents %s"
        % (username, jobname, agent_ids)
    )

    logger.info(msg)
    if agent_ids:
        for agent_id in agent_ids:
            if job_info['operation'] == 'install':
                if not job_info.get('app_ids', None):
                    job_info['app_ids'] = get_agent_apps_details(
                        job_info, agent_id, details=False
                    )

                if job_info['app_ids']:
                    logger.debug("About to execute the job %s" % (job_info))

        if job_info['pkg_type'] == 'system_apps':
            oper = store_operation.install_os_apps(
                job_info['app_ids'],
                agentids=job_info['agent_ids'],
                tag_id=job_info['tag_ids'],
                restart=None
            )
            logger.debug(oper)

        elif job_info['pkg_type'] == 'custom_apps':
            oper = store_operation.install_custom_apps(
                job_info['app_ids'],
                agentids=job_info['agent_ids'],
                tag_id=job_info['tag_ids'],
                restart=None
            )
            logger.debug(oper)

        elif job_info['pkg_type'] == 'supported_apps':
            oper = store_operation.install_supported_apps(
                job_info['app_ids'],
                agentids=job_info['agent_ids'],
                tag_id=job_info['tag_ids'],
                restart=None
            )

            logger.debug(oper)


def scheduled_reboot_operation(job_info, view_name, username,
        uri=None, method=None, conn=None):

    store_operation = StoreAgentOperation(
        username, view_name, uri, method
    )

    operation = job_info['operation']
    agent_ids = get_agentids_per_job(
        job_info=job_info, username=username, view_name=view_name
    )

    if operation == 'reboot':
        logger.debug(" About to execute the job %s" % (job_info))
        oper = store_operation.reboot(agentids=agent_ids)
        logger.debug(oper)

def scheduled_shutdown_operation(job_info, view_name, username,
        uri=None, method=None, conn=None):

    store_operation = StoreAgentOperation(
        username, view_name, uri, method
    )

    operation = job_info['operation']
    agent_ids = get_agentids_per_job(
        job_info=job_info, username=username, view_name=view_name
    )

    if operation == 'shutdown':
        logger.debug(" About to execute the job %s" % (job_info))
        oper = store_operation.shutdown(agentids=agent_ids)
        logger.debug(oper)


def operation_info(operation=None, pkg_type=None, severity=None,
        tag_ids=None, agent_ids=None, name=None, all_agents=None, all_tags=None
        ):

    jobby_job = {
        'operation': operation,
        'pkg_type': pkg_type,
        'severity': severity,
        'tag_ids': tag_ids,
        'agent_ids': agent_ids,
        'job_name': name,
        'all_agents': all_agents,
        'all_tags': all_tags,
    }

    return(jobby_job)


def add_yearly_recurrent(sched, view_name, username,
        agent_ids=None, all_agents=None, tag_ids=None, all_tags=None,
        severity=None, pkg_type=None, operation=None, name=None, custom=None,
        every=None, epoch_time=None, uri=None, method=None):

    if epoch_time:
        date=datetime.fromtimestamp(int(epoch_time))
        year = date.year
        month = date.month
        day = date.day
        hour = date.hour
        minute = date.minute

    succeeded = False
    msg = 'Recurrent Scheduled added successfully'

    job_data = {
           'month': month,
           'day': day,
           'hour': hour,
           'minute': minute,
           'job_name': name,
           'all_tags': all_tags,
           'all_agents': all_agents,
           'agent_ids': agent_ids,
           'tag_ids': tag_ids,
           'operation': operation,
           'severity': severity,
           'pkg_type': pkg_type,
           'view_name': view_name,
           'created_by': username
    }

    if name:
        job_exist = job_exists(
            sched=sched, jobname=name,
            username=username, view_name=view_name,
        )

        if job_exist:
            msg = 'Job Name Already Exists. Job Can not be Added'
            results = SchedulerResults(
                username, uri, method
            ).exists(name)

        else:
            jobby_job = operation_info(
                operation=operation, pkg_type=pkg_type,
                agent_ids=agent_ids,
                all_agents=all_agents,tag_ids=tag_ids,
                all_tags=all_tags, severity=severity, name=name
            )

            if jobby_job['operation'] == 'install':

                try:
                    if not custom and not every:
                        sched.add_cron_job(
                            scheduled_install_operation,month=month,
                            day=day, hour=hour, minute=minute,
                            args=[jobby_job, view_name, username],
                            name=name, jobstore=view_name
                        )

                    elif custom and every:
                        year = ("{0}/{1}".format(year,every))
                        month = (",".join(custom))
                        sched.add_cron_job(
                            scheduled_install_operation, year = year,
                            month=month, day=day, hour=hour, minute=minute,
                            args=[jobby_job, view_name, username],
                            name=name, jobstore=view_name
                        )

                    results = SchedulerResults(
                        username, uri, method
                    ).created(name, job_data)

                except Exception as e:
                    logger.exception(e)
                    results = GenericResults(
                        username, uri, method
                    ).something_broke(name, 'adding schedule', e)


            elif jobby_job['operation'] == 'reboot':
                try:
                    if not custom and not every:
                        sched.add_cron_job(
                            scheduled_reboot_operation, month=month,
                            hour=hour,minute=minute,
                            args=[jobby_job, view_name, username],
                            name=name, jobstore=view_name
                        )

                    elif custom and every:
                        year = ("{0}/{1}".format(year,every))
                        month = (",".join(custom))
                        sched.add_cron_job(
                            scheduled_reboot_operation, year = year,
                            month=month, day=day, hour=hour, minute=minute,
                            args=[jobby_job, view_name, username],
                            name=name, jobstore=view_name
                        )

                    results = SchedulerResults(
                        username, uri, method
                    ).created(name, job_data)

                except Exception as e:
                    logger.exception(e)
                    results = GenericResults(
                        username, uri, method
                    ).something_broke(name, 'adding schedule', e)

            elif jobby_job['operation'] == 'shutdown':
                try:
                    if not custom and not every:
                        sched.add_cron_job(
                            scheduled_shutdown_operation, month=month,
                            hour=hour,minute=minute,
                            args=[jobby_job, view_name, username],
                            name=name, jobstore=view_name
                        )

                    elif custom and every:
                        year = ("{0}/{1}".format(year,every))
                        month = (",".join(custom))
                        sched.add_cron_job(
                            scheduled_shutdown_operation, year = year,
                            month=month, day=day, hour=hour, minute=minute,
                            args=[jobby_job, view_name, username],
                            name=name, jobstore=view_name
                        )

                    results = SchedulerResults(
                        username, uri, method
                    ).created(name, job_data)

                except Exception as e:
                    logger.exception(e)
                    results = GenericResults(
                        username, uri, method
                    ).something_broke(name, 'adding schedule', e)

    return results


def add_monthly_recurrent(sched, view_name, username,
        agent_ids=None, all_agents=None, tag_ids=None, all_tags=None,
        severity=None, pkg_type=None, operation=None, name=None, custom=None,
        every=None, epoch_time=None, uri=None, method=None):

    if epoch_time:
        date=datetime.fromtimestamp(int(epoch_time))
        month = date.month
        day = date.day
        hour = date.hour
        minute = date.minute


    job_data = (
        {
           'day': day,
           'hour': hour,
           'minute': minute,
           'job_name': name,
           'all_tags': all_tags,
           'all_agents': all_agents,
           'agent_ids': agent_ids,
           'tag_ids': tag_ids,
           'operation': operation,
           'severity': severity,
           'pkg_type': pkg_type,
           'view_name': view_name,
           'created_by': username
        }
    )

    if name:
        job_exist = job_exists(
            sched=sched, jobname=name,
            view_name=view_name, username=username
        )

        if job_exist:
            msg = 'Job Name Already Exists. Job Can not be Added'
            results = SchedulerResults(
                username, uri, method
            ).exists(name)

        else:
            jobby_job = operation_info(
                operation=operation, pkg_type=pkg_type, agent_ids=agent_ids,
                all_agents=all_agents,tag_ids=tag_ids,
                all_tags=all_tags, severity=severity, name=name
            )

            if jobby_job['operation'] == 'install':

                try:
                    if not custom and not every:
                        sched.add_cron_job(
                            scheduled_install_operation,
                            day=day, hour=hour, minute=minute,
                            args=[jobby_job, view_name, username],
                            name=name, jobstore=view_name
                        )

                    elif custom and every:
                        month = ("{0}/{1}".format(month,every))
                        day = (",".join(custom))
                        sched.add_cron_job(
                            scheduled_install_operation,month = month,
                            day=day, hour=hour, minute=minute,
                            args=[jobby_job, view_name, username],
                            name=name, jobstore=view_name
                        )

                    results = SchedulerResults(
                        username, uri, method
                    ).created(name, job_data)

                except Exception as e:
                    logger.exception(e)
                    results = GenericResults(
                        username, uri, method
                    ).something_broke(name, 'adding schedule', e)

            elif jobby_job['operation'] == 'reboot':
                try:
                    if not custom and not every:
                        sched.add_cron_job(
                            scheduled_reboot_operation, day=day,
                            hour=hour,minute=minute,
                            args=[jobby_job, view_name, username],
                            name=name, jobstore=view_name
                        )

                    elif custom and every:
                        month = ("{0}/{1}".format(month,every))
                        day = (",".join(custom))
                        sched.add_cron_job(
                            scheduled_reboot_operation, month=month,
                            day=day, hour=hour,minute=minute,
                            args=[jobby_job, view_name, username],
                            name=name, jobstore=view_name
                        )

                    results = SchedulerResults(
                        username, uri, method
                    ).created(name, job_data)

                except Exception as e:
                    logger.exception(e)
                    results = GenericResults(
                        username, uri, method
                    ).something_broke(name, 'adding schedule', e)

            elif jobby_job['operation'] == 'shutdown':
                try:
                    if not custom and not every:
                        sched.add_cron_job(
                            scheduled_shutdown_operation, day=day,
                            hour=hour,minute=minute,
                            args=[jobby_job, view_name, username],
                            name=name, jobstore=view_name
                        )

                    elif custom and every:
                        month = ("{0}/{1}".format(month,every))
                        day = (",".join(custom))
                        sched.add_cron_job(
                            scheduled_shutdown_operation, month=month,
                            day=day, hour=hour,minute=minute,
                            args=[jobby_job, view_name, username],
                            name=name, jobstore=view_name
                        )

                    results = SchedulerResults(
                        username, uri, method
                    ).created(name, job_data)

                except Exception as e:
                    logger.exception(e)
                    results = GenericResults(
                        username, uri, method
                    ).something_broke(name, 'adding schedule', e)

    return results

def add_daily_recurrent(sched, view_name, username,
        agent_ids=None, all_agents=None,tag_ids=None, all_tags=None,
        severity=None, pkg_type=None, operation=None, name=None, every=None,
        custom=None, epoch_time=None, uri=None, method=None):

    if epoch_time:
        date=datetime.fromtimestamp(int(epoch_time))
        day = date.day
        hour = date.hour
        minute = date.minute

    job_data = (
        {
           'hour': hour,
           'minute': minute,
           'job_name': name,
           'all_tags': all_tags,
           'all_agents': all_agents,
           'agent_ids': agent_ids,
           'tag_ids': tag_ids,
           'operation': operation,
           'severity': severity,
           'pkg_type': pkg_type,
           'view_name': view_name,
           'created_by': username
        }
    )

    succeeded = False
    msg = 'Recurrent Scheduled added successfully'

    if name:
        job_exist = job_exists(
            sched=sched, jobname=name,
            view_name=view_name, username=username
        )

        if job_exist:
            msg = 'Job Name Already Exists. Job Can not be Added'
            results = SchedulerResults(
                username, uri, method
            ).exists(name)

        else:
            jobby_job = operation_info(
                operation=operation, pkg_type=pkg_type, agent_ids=agent_ids,
                all_agents=all_agents,tag_ids=tag_ids,
                all_tags=all_tags, severity=severity, name=name
            )

            if jobby_job['operation'] == 'install':
                try:
                    if not custom and not every:
                        sched.add_cron_job(
                            scheduled_install_operation,
                            hour=hour, minute=minute,
                            args=[jobby_job, view_name, username],
                            name=name, jobstore=view_name
                        )

                    elif every:
                        day = ("{0}/{1}".format(date.day, every))
                        sched.add_cron_job(
                            scheduled_install_operation,
                            day = day, hour=hour, minute=minute,
                            args=[jobby_job, view_name, username],
                            name=name, jobstore=view_name
                        )

                    results = SchedulerResults(
                        username, uri, method
                    ).created(name, job_data)

                except Exception as e:
                    logger.exception(e)
                    results = GenericResults(
                        username, uri, method
                    ).something_broke(name, 'adding schedule', e)

            elif jobby_job['operation'] == 'reboot':
                try:
                    if not custom and not every:
                        sched.add_cron_job(
                            scheduled_reboot_operation,
                            hour=hour, minute=minute,
                            args=[jobby_job, view_name, username],
                            name=name, jobstore=view_name
                        )

                    elif every:
                        day = ("{0}/{1}".format(date.day, every))
                        sched.add_cron_job(
                            scheduled_reboot_operation,
                            day = day, hour=hour, minute=minute,
                            args=[jobby_job, view_name, username],
                            name=name, jobstore=view_name
                        )

                    results = SchedulerResults(
                        username, uri, method
                    ).created(name, job_data)

                except Exception as e:
                    logger.exception(e)
                    results = GenericResults(
                        username, uri, method
                    ).something_broke(name, 'adding schedule', e)

            elif jobby_job['operation'] == 'shutdown':
                try:
                    if not custom and not every:
                        sched.add_cron_job(
                            scheduled_shutdown_operation,
                            hour=hour, minute=minute,
                            args=[jobby_job, view_name, username],
                            name=name, jobstore=view_name
                        )

                    elif every:
                        day = ("{0}/{1}".format(date.day, every))
                        sched.add_cron_job(
                            scheduled_shutdown_operation,
                            day = day, hour=hour, minute=minute,
                            args=[jobby_job, view_name, username],
                            name=name, jobstore=view_name
                        )

                    results = SchedulerResults(
                        username, uri, method
                    ).created(name, job_data)

                except Exception as e:
                    logger.exception(e)
                    results = GenericResults(
                        username, uri, method
                    ).something_broke(name, 'adding schedule', e)

    return results


def add_weekly_recurrent(sched, view_name, username,
        agent_ids=None, all_agents=None,tag_ids=None, all_tags=None,
        severity=None, pkg_type=None, operation=None, name=None, every=None,
        custom=None, epoch_time=None, uri=None, method=None):

    if epoch_time:
        date=datetime.fromtimestamp(int(epoch_time))
        week_num= (date.isocalendar()[1])
        week=("{0}/{1}".format(week_num,every))
        day_of_week = date.weekday()
        hour = date.hour
        minute = date.minute

    job_data = (
        {
           'hour': hour,
           'minute': minute,
           'day_of_week': day_of_week,
           'week_num':week_num,
           'job_name': name,
           'all_tags': all_tags,
           'all_agents': all_agents,
           'agent_ids': agent_ids,
           'tag_ids': tag_ids,
           'operation': operation,
           'severity': severity,
           'pkg_type': pkg_type,
           'view_name': view_name,
           'created_by': username
        }
    )
    succeeded = False
    msg = 'Recurrent Scheduled added successfully'

    if name:
        job_exist = job_exists(sched=sched, jobname=name,
                                username=username,
                                view_name=view_name
                                )
        if job_exist:
            msg = 'Job Name Already Exists. Job Can not be Added'
            results = (
                 SchedulerResults(
                     username, uri, method
                 ).exists(name)
            )

        else:
            jobby_job = (
                operation_info(
                    operation=operation, pkg_type=pkg_type, agent_ids=agent_ids,
                    all_agents=all_agents,tag_ids=tag_ids,
                    all_tags=all_tags, severity=severity, name=name
                )
            )
            if jobby_job['operation'] == 'install':
                try:
                    if not custom and not every:
                        sched.add_cron_job(
                            scheduled_install_operation, day_of_week=day_of_week,
                            hour=hour, minute=minute,
                            args=[jobby_job, view_name, username],
                            name=name, jobstore=view_name
                            )

                    elif custom and every:
                        week=("{0}/{1}".format(week_num,every))
                        day_of_week = (",".join(custom))

                        sched.add_cron_job(
                            scheduled_install_operation, week=week,
                            day_of_week=day_of_week,
                            hour=hour, minute=minute,
                            args=[jobby_job, view_name, username],
                            name=name, jobstore=view_name
                            )

                    results = (
                            SchedulerResults(
                                username, uri, method
                                ).created(name, job_data)
                            )

                except Exception as e:
                    logger.exception(e)
                    results = (
                        GenericResults(
                            username, uri, method
                        ).something_broke(name, 'adding schedule', e)
                    )

            elif jobby_job['operation'] == 'reboot':

                try:
                    if not custom:
                        sched.add_cron_job(
                                scheduled_reboot_operation,day_of_week=day_of_week,
                                hour=hour, minute=minute,
                                args=[jobby_job, view_name, username],
                                name=name, jobstore=view_name
                                )
                    elif custom:
                        sched.add_cron_job(
                                scheduled_reboot_operation, week=week,
                                day_of_week=day_of_week,
                                hour=hour, minute=minute,
                                args=[jobby_job, view_name, username],
                                name=name, jobstore=view_name
                                )

                    results = (
                        SchedulerResults(
                            username, uri, method
                        ).created(name, job_data)
                    )

                except Exception as e:
                    logger.exception(e)
                    results = (
                        GenericResults(
                            username, uri, method
                        ).something_broke(name, 'adding schedule', e)
                    )


            elif jobby_job['operation'] == 'shutdown':

                try:
                    if not custom:
                        sched.add_cron_job(
                                scheduled_shutdown_operation,day_of_week=day_of_week,
                                hour=hour, minute=minute,
                                args=[jobby_job, view_name, username],
                                name=name, jobstore=view_name
                                )
                    elif custom:
                        sched.add_cron_job(
                                scheduled_shutdown_operation, week=week,
                                day_of_week=day_of_week,
                                hour=hour, minute=minute,
                                args=[jobby_job, view_name, username],
                                name=name, jobstore=view_name
                                )

                    results = (
                        SchedulerResults(
                            username, uri, method
                        ).created(name, job_data)
                    )

                except Exception as e:
                    logger.exception(e)
                    results = (
                        GenericResults(
                            username, uri, method
                        ).something_broke(name, 'adding schedule', e)
                    )

    return(results)


def schedule_once(sched, view_name, username,
      agent_ids=None, all_agents=None, tag_ids=None, all_tags=None,
      severity=None, pkg_type=None, operation=None, name=None, date=None,
      uri=None, method=None, job_extra=None):

    job_data = (
        {
           'job_name': name,
           'all_tags': all_tags,
           'all_agents': all_agents,
           'agent_ids': agent_ids,
           'tag_ids': tag_ids,
           'operation': operation,
           'severity': severity,
           'pkg_type': pkg_type,
           'view_name': view_name,
           'created_by': username
        }
    )
    succeeded = False
    msg = 'Recurrent Scheduled added successfully'
    if name:
        job_exist = job_exists(
            sched=sched, jobname=name, view_name=view_name,
            username=username
        )

        if job_exist:
            msg = 'Job Name Already Exists. Job Can not be Added'
            results = SchedulerResults(username, uri, method).exists(name)

        else:
            jobby_job = (
                operation_info(
                    operation=operation,
                    pkg_type=pkg_type,
                    agent_ids=agent_ids,
                    all_agents=all_agents,
                    tag_ids=tag_ids,
                    all_tags=all_tags,
                    severity=severity,
                    name=name
                )
            )
            if job_extra:
                for key, val in job_extra.items():
                    jobby_job[key] = val

            if jobby_job['operation'] == 'install':
                try:
                    sched.add_date_job(
                        scheduled_install_operation,
                        date=date,
                        args=[jobby_job, view_name, username],
                        name=name,
                        jobstore=view_name
                    )

                    results = SchedulerResults(
                        username, uri, method
                    ).created(name, job_data)

                except Exception as e:
                    logger.exception(e)
                    results = GenericResults(
                        username, uri, method
                    ).something_broke(name, 'adding schedule', e)

            elif jobby_job['operation'] == 'reboot':

                try:
                    sched.add_date_job(
                        scheduled_reboot_operation,
                        date=date,
                        args=[jobby_job, view_name, username],
                        name=name, jobstore=view_name
                    )
                    results = SchedulerResults(
                        username, uri, method
                    ).created(name, job_data)

                except Exception as e:
                    logger.exception(e)
                    results = GenericResults(
                        username, uri, method
                    ).something_broke(name, 'adding schedule', e)

            elif jobby_job['operation'] == 'shutdown':

                try:
                    sched.add_date_job(
                        scheduled_shutdown_operation,
                        date=date,
                        args=[jobby_job, view_name, username],
                        name=name, jobstore=view_name
                    )
                    results = SchedulerResults(
                        username, uri, method
                    ).created(name, job_data)

                except Exception as e:
                    logger.exception(e)
                    results = GenericResults(
                        username, uri, method
                    ).something_broke(name, 'adding schedule', e)

    return(results)


def add_custom_recurrent(sched, view_name, username,
        agent_ids=None, all_agents=None, tag_ids=None, all_tags=None,
        severity=None, pkg_type=None, operation=None, name=None, uri=None,
        method=None, date=None, every=None, custom=None, frequency=None):

    job_data = (
        {
           'date': date,
           'frequency': frequency,
           'every': every,
           'job_name': name,
           'all_tags': all_tags,
           'all_agents': all_agents,
           'agent_ids': agent_ids,
           'tag_ids': tag_ids,
           'operation': operation,
           'severity': severity,
           'pkg_type': pkg_type,
           'view_name': view_name,
           'created_by': username
        }
    )

    if name:
        job_exist = job_exists(sched=sched, jobname=name,
            view_name=view_name, username=username
        )

        if job_exist:
            msg = 'Job Name Already Exists. Job Can not be Added'
            results = SchedulerResults(username, uri, method).exists(name)

        else:
            jobby_job = (
                operation_info(
                    operation=operation,
                    pkg_type=pkg_type,
                    agent_ids=agent_ids,
                    all_agents=all_agents,
                    tag_ids=tag_ids,
                    all_tags=all_tags,
                    severity=severity,
                    name=name
                )
            )

            if frequency == 'daily':
                day=("{0}/{1}".format(date.day,every))
                if jobby_job['operation'] == 'install':
                    try:

                        sched.add_cron_job(
                            scheduled_install_operation,
                            day = day,
                            hour=hour,
                            minute=minute,
                            args=[jobby_job, view_name, username],
                            name=name,
                            jobstore=view_name
                        )

                        results = SchedulerResults(
                            username, uri, method
                        ).created(name, job_data)

                    except Exception as e:
                        logger.exception(e)
                        results = GenericResults(
                            username, uri, method
                        ).something_broke(name, 'adding schedule', e)

                elif jobby_job['operation'] == 'reboot':
                    try:
                        sched.add_cron_job(
                            scheduled_reboot_operation,
                            day = day,
                            hour=date.hour,
                            minute=date.minute,
                            args=[jobby_job, view_name, username],
                            name=name,
                            jobstore=view_name
                        )

                        results = SchedulerResults(
                            username, uri, method
                        ).created(name, job_data)

                    except Exception as e:
                        logger.exception(e)
                        results = (
                                GenericResults(
                                    username, uri, method
                                    ).something_broke(name, 'adding schedule', e)
                                )
            elif frequency == 'weekly':
                week_num= (date.isocalendar()[1])
                week=("{0}/{1}".format(week_num,every))
                if jobby_job['operation'] == 'install':
                    try:
                        sched.add_cron_job(
                            scheduled_install_operation, week=week,
                            day_of_week=custom, hour=date.hour,
                            minute=date.minute,
                            args=[jobby_job, view_name, username],
                            name=name, jobstore=view_name
                        )

                        results = SchedulerResults(
                            username, uri, method
                        ).created(name, job_data)

                    except Exception as e:
                        logger.exception(e)
                        results = GenericResults(
                            username, uri, method
                        ).something_broke(name, 'adding schedule', e)

                elif jobby_job['operation'] == 'reboot':
                    try:
                        sched.add_cron_job(
                            scheduled_reboot_operation, week = week,
                            day_of_week=custom, hour=date.hour,
                            minute=date.minute,
                            args=[jobby_job, view_name, username],
                            name=name, jobstore=view_name
                        )

                        results = SchedulerResults(
                            username, uri, method
                        ).created(name, job_data)

                    except Exception as e:
                        logger.exception(e)
                        results = GenericResults(
                            username, uri, method
                        ).something_broke(name, 'adding schedule', e)

            elif frequency == 'monthly':
                month=("{0}/{1}".format(date.month,every))
                if jobby_job['operation'] == 'install':
                    try:
                        sched.add_cron_job(
                            scheduled_install_operation, month = month,
                            day = custom, hour=date.hour, minute=date.minute,
                            args=[jobby_job, view_name, username],
                            name=name, jobstore=view_name
                        )

                        results = SchedulerResults(
                            username, uri, method
                        ).created(name, job_data)

                    except Exception as e:
                        logger.exception(e)
                        results = GenericResults(
                            username, uri, method
                        ).something_broke(name, 'adding schedule', e)

                elif jobby_job['operation'] == 'reboot':
                    try:
                        sched.add_cron_job(
                            scheduled_reboot_operation, month = month,
                            day=custom,hour=date.hour, minute=date.minute,
                            args=[jobby_job, view_name, username],
                            name=name, jobstore=view_name
                        )

                        results = SchedulerResults(
                            username, uri, method
                        ).created(name, job_data)

                    except Exception as e:
                        logger.exception(e)
                        results = GenericResults(
                            username, uri, method
                        ).something_broke(name, 'adding schedule', e)

            elif frequency=='yearly':
                year=("{0}/{1}".format(date.year,every))
                if jobby_job['operation'] == 'install':
                    try:
                        sched.add_cron_job(
                            scheduled_install_operation, year = year,
                            month = custom, day = date.day, hour=date.hour,
                            minute=date.minute,
                            args=[jobby_job, view_name, username],
                            name=name, jobstore=view_name
                        )

                        results = SchedulerResults(
                            username, uri, method
                        ).created(name, job_data)

                    except Exception as e:
                        logger.exception(e)
                        results = GenericResults(
                            username, uri, method
                        ).something_broke(name, 'adding schedule', e)

                elif jobby_job['operation'] == 'reboot':
                    try:
                        sched.add_cron_job(
                            scheduled_reboot_operation, year=year,
                            month = custom,day=custom,hour=date.hour,
                            minute=date.minute,
                            args=[jobby_job, view_name, username],
                            name=name, jobstore=view_name
                        )

                        results = SchedulerResults(
                            username, uri, method
                        ).created(name, job_data)

                    except Exception as e:
                        logger.exception(e)
                        results = GenericResults(
                            username, uri, method
                        ).something_broke(name, 'adding schedule', e)
