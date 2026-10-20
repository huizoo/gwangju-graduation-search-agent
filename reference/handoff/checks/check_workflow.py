"""Executable recovery specification; not a worker implementation."""
import json

TERMINAL = {'cancelled', 'completed', 'failed'}

def recover_job_status(status, all_items_done=False):
    if status == 'cancelling':
        return 'cancelled'
    if status in {'running', 'pausing'}:
        return 'completed' if all_items_done else 'paused'
    return status


def apply_late_result(status, expected_revision, current_revision):
    return status not in TERMINAL | {'cancelling'} and expected_revision == current_revision


def current_confirmation_counts(events, eligible_ids):
    eligible = set(eligible_ids)
    date_ids = {e['institution']['id'] for e in events if e['publication']['current_date_allowed']} & eligible
    time_ids = {e['institution']['id'] for e in events if e['publication']['current_time_allowed']} & eligible
    assert time_ids <= date_ids
    return {'eligible': len(eligible), 'date': len(date_ids), 'time': len(time_ids),
            'date_ratio': len(date_ids) / len(eligible) if eligible else None,
            'time_ratio': len(time_ids) / len(eligible) if eligible else None}


def main():
    for status in TERMINAL | {'queued', 'paused'}:
        assert recover_job_status(status) == status
    assert recover_job_status('cancelling') == recover_job_status('cancelling', all_items_done=True) == 'cancelled'
    assert recover_job_status('running', all_items_done=True) == 'completed'
    assert recover_job_status('running') == recover_job_status('pausing') == 'paused'
    for status in TERMINAL | {'cancelling'}:
        assert not apply_late_result(status, 1, 1)
    assert not apply_late_result('running', 1, 2)
    assert apply_late_result('running', 2, 2)
    def event(i,d,t):return {'institution':{'id':i},'publication':{'current_date_allowed':d,'current_time_allowed':t}}
    assert current_confirmation_counts([event('a',True,False),event('a',True,True),event('b',False,False)],['a','b']) == {'eligible':2,'date':1,'time':1,'date_ratio':.5,'time_ratio':.5}
    assert current_confirmation_counts([],[]) == {'eligible':0,'date':0,'time':0,'date_ratio':None,'time_ratio':None}
    assert current_confirmation_counts([event('history',False,False)],['history'])['date']==0
    print(json.dumps({'version':'4.0','recovery':'passed','late_responses':'passed','institution_dedup_and_current_metrics':'passed','scope':'reference specification only'}))

if __name__ == '__main__':main()
