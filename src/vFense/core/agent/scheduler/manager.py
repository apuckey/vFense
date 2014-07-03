from vFense.core.operation._constants import AgentOperations
from vFense.core.scheduler import Job
from vFense.core.scheduler._constants import (
    ScheduleKeys, ScheduleTriggers
)
from vFense.core.scheduler.manager import JobManager
from vFense.core.agent.operations.store_agent_operations import (
    StoreAgentOperations
)

def reboot_agents(agents=None, view_name=None, user_name=None):
    """Reboot agents
    Kwargs:
        agents (list): List of agent ids.
        view_name (str): The name of the view, this operation is being
            performed on.
        user_name (str): The user who performed this operation.
    """
    operation = StoreAgentOperations(user_name, view_name)
    operation.reboot(agentids=agents)
    pass



class AgentJobManager(JobManager):
    def reboot_once(self, start_date, agents, label,
                    user_name, time_zone=None):
        """Reboot 1 or multiple agents once.
        Args:
            job (Job): Instance of Job.
            agents (list): List of agent ids.
        """
        job_kwargs = {
            ScheduleKeys.Agents: agents,
            ScheduleKeys.UserName: user_name,
            ScheduleKeys.ViewName: self.view_name,
        }
        job = (
            Job(
                label, reboot_agents, job_kwargs, start_date,
                operation=AgentOperations.REBOOT, time_zone=time_zone,
                trigger=ScheduleTriggers.DATE
            )
        )
        results = self.add_date_job(job)
        return results

    def reboot_cron(self, start_date, agents, label,
                    user_name, time_zone=None):
        """Reboot 1 or multiple agents on an interval.
        Args:
            job (Job): Instance of Job.
            agents (list): List of agent ids.
        """
        job_kwargs = {
            ScheduleKeys.Agents: agents,
            ScheduleKeys.UserName: user_name,
            ScheduleKeys.ViewName: self.view_name,
        }
        job = (
            Job(
                label, reboot_agents, job_kwargs, start_date,
                operation=AgentOperations.REBOOT, time_zone=time_zone,
                trigger=ScheduleTriggers.DATE
            )
        )
        results = self.add_date_job(job)
        return results