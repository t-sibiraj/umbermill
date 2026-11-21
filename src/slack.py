"""Use already-paid worker capacity for extra tests with observed file dependencies."""
import heapq
from .scheduler import _milliseconds


class SlackPacker:
    def __init__(self,durations):
        self.cost={n:_milliseconds(s) for n,s in durations.items()}
        self.order=sorted(self.cost,key=lambda n:(self.cost[n],n))

    def fill(self,schedule,possible):
        if not schedule:
            return schedule
        loads=[sum(self.cost[n] for n in w) for w in schedule]
        max_load=max(loads)
        if max_load==min(loads) and self.cost[self.order[0]]>0:
            return schedule
        queue=list(zip(loads,range(len(schedule))))
        heapq.heapify(queue)
        used={n for w in schedule for n in w}
        final=[list(worker) for worker in schedule]
        for n in self.order:
            duration=self.cost[n]
            load,worker=queue[0]
            if load+duration>max_load:
                break
            if n not in used and (possible >> n) & 1:
                final[worker].append(n)
                heapq.heapreplace(queue,(load+duration,worker))
        return final
