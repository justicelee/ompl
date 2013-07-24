
import socket

from ompl import base as ob
from ompl import morse as om
from ompl import util as ou

def list2vec(l):
    """
    Convert a Python list into an ou.vectorDouble.
    """
    ret = ou.vectorDouble()
    for e in l:
        ret.append(e)
    return ret
    
class MyEnvironment(om.MorseEnvironment):
    """
    Represents the MORSE environment we will be planning in.
    Inherits from the C++ OMPL class ob.MorseEnvironment and
    implements pure virtual functions readState(),
    writeState(), applyControl(), and worldStep().
    """
    
    def __init__(self, state_socket, control_socket):
        """
        Get information from Blender about the scene.
        """
        self.sockS = state_socket
        self.sockC = control_socket
        self.simRunning = True
        
        self.cdesc = self.call('getControlDescription()')   # control dimension and info for applying controls
        print(self.cdesc)
        self.con = [0 for _ in range(self.cdesc[0])]    # cache of the last control set to MORSE
        cb = [self.cdesc[0], list2vec([-10,10,-1,1])]   # TODO get bounds from user
        
        rb = self.call('getRigidBodiesBounds()')    # number of bodies and positional bounds
        rb[1] = list2vec(rb[1])
        inf = float('inf')
        rb.append(list2vec([-inf, inf, -inf, inf, -inf, inf])) # lin bounds
        rb.append(list2vec([-inf, inf, -inf, inf, -inf, inf])) # ang bounds
        
        envArgs = cb + rb + [0.1, 5, 30]    # step size, min/max control durations
        super(MyEnvironment, self).__init__(*envArgs)
        
        # tell MORSE to reset the simulation, because it was running while it was initializing
        self.sockC.sendall(b'id simulation reset_objects')
        
        
    def call(self, cmd):
        """
        Request a function call cmd from the simulation and
        return the result.
        """
        # submit cmd to socket; return eval()'ed response
        try:
            self.sockS.sendall(cmd.encode())
            return eval(self.sockS.recv(16384))    # TODO: buffer size? states can get pretty big
        except:
            self.simRunning = False
            raise
    
    def getGoalCriteria(self):
        """
        Get a list of tuples [(i_0,state_0),...] where i_n is
        the index of a rigid body in the world state and
        state_n is its goal position.
        """
        return self.call('getGoalCriteria()')
    
    def readState(self, state):
        """
        Get the state from the simulation so OMPL can use it.
        """
        simState = self.call('extractState()')
        i = 0
        for obj in simState:
            # for each rigid body
            for j in range(3):
                # copy a 3-vector (pos, lin, ang)
                for k in range(3):
                    state[i][k] = obj[j][k]
                i += 1
            # copy a 4-vector into the quaternion
            state[i].w = obj[3][0]
            state[i].x = obj[3][1]
            state[i].y = obj[3][2]
            state[i].z = obj[3][3]
            i += 1
    
    def stateToList(self, state):
        simState = []
        for i in range(0, self.rigidBodies_*4, 4):
            # for each body
            simState.append((
                (state[i][0], state[i][1], state[i][2]),
                (state[i+1][0], state[i+1][1], state[i+1][2]),
                (state[i+2][0], state[i+2][1], state[i+2][2]),
                (state[i+3].w, state[i+3].x, state[i+3].y, state[i+3].z)
            ))
        
        return simState
    
    def writeState(self, state):
        """
        Compose a state string from the state data
        and send it to the simulation.
        """
        # make safe for eval()
        s = repr(self.stateToList(state))
        s = s.replace('nan','float("nan")')
        s = s.replace('inf','float("inf")')
        
        # send it to the simulation
        self.call('submitState(%s)' % s)
        
    def applyControl(self, control):
        """
        Tell MORSE to apply control to the robot.
        """
        con = [control[i] for i in range(len(control))] # make it iterable
        # If the control hasn't changed, we don't need to do anything
        if self.con != con:
            self.con = con
            i = 0
            for controller in self.cdesc[1:]:
                req = 'id %s %s %s\n' % (controller[0], controller[1], con[i:i+controller[2]])
                i += controller[2]
                self.sockC.sendall(req.encode())
        
    def worldStep(self, dur):
        """
        Run the simulation for dur seconds.
        """
        for i in range(int(round(dur/(1.0/60)))):
            self.call('nextTick()')
        
    def endSimulation(self):
        """
        Let the simulation know to shut down.
        """
        if self.simRunning:
            self.call('endSimulation()')

class MyProjection(om.MorseProjection):
    """
    The projection evaluator for the simulation. Uses the x and y coordinates
    in the position component of the robot.
    """
    
    def __init__(self, space):
        super(MyProjection, self).__init__(space)
        self.bounds_ = ob.RealVectorBounds(self.getDimension())
        self.robotPosSpaceIndex = 4 # TODO: figure out automatically which components are the robot positions
        for i in range(self.getDimension()):
            self.bounds_.low[i] = space.getSubspace(self.robotPosSpaceIndex).getBounds().low[i]
            self.bounds_.high[i] = space.getSubspace(self.robotPosSpaceIndex).getBounds().high[i]
        self.defaultCellSizes()
    
    def getDimension(self):
        return 2
    
    def defaultCellSizes(self):
        # grid for robot x,y locations
        self.cellSizes_ = list2vec([2,2])
    
    def project(self, state, projection):
        # use x and y coords of the robots
        projection[0] = state[self.robotPosSpaceIndex][0]
        projection[1] = state[self.robotPosSpaceIndex][1]

class MyGoal(om.MorseGoal):
    """
    The goal state of the simulation.
    """
    def __init__(self, si, env):
        """
        Initialize the goal and get list of criteria for satisfaction.
        """
        super(MyGoal, self).__init__(si)
        self.criteria = env.getGoalCriteria()
    
    def dist(self, s1, s2):
        """
        How close are these rigid body states (position and orientation)?
        Calculates Euclidean distance between positions and distance between
        orientations as 1-(<q1,q2>^2) where <q1,q2> is the inner product of the quaternions.
        """
        return sum((s1[0][i]-s2[0][i])**2 for i in range(3)) + \
            (1 - sum(s1[3][i]*s2[3][i] for i in range(4))**2)   # value in [0,1] where 0 means quats are the same
    
    def isSatisfied_Py(self, state):
        """
        For every goal object, check if the rigid body object is close
        """
        self.distance = 0
        for crit in self.criteria:
            quat = state[4*crit[0]+3]
            stateTup = (state[4*crit[0]+0], state[4*crit[0]+1],
                        state[4*crit[0]+2], (quat.w, quat.x, quat.y, quat.z))
            self.distance += self.dist(stateTup, crit[1])
        
        if self.distance > len(crit)*0.1:
            return False
        return True

